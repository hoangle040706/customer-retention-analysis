"""
Production-Ready Customer Retention & RFM Pipeline
Automates data cleaning, return matching, cohort indexing, and 25-cell RFM segmentation.
"""

import sys
import time
from pathlib import Path
import numpy as np
import pandas as pd

# Define paths
BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DATA_PATH = BASE_DIR / "data" / "raw" / "Online Retail.xlsx"
PROCESSED_DIR = BASE_DIR / "data" / "processed"

REF_DATE = pd.to_datetime("2011-12-10")

def run_cleaning_pipeline():
    print("=" * 60)
    print("1. RUNNING DATA CLEANING & MATCHED RETURNS PIPELINE")
    print("=" * 60)
    
    if not RAW_DATA_PATH.exists():
        raise FileNotFoundError(f"Raw data not found at: {RAW_DATA_PATH}")
    
    start_t = time.time()
    print(f"Reading raw dataset from: {RAW_DATA_PATH}...")
    df_raw = pd.read_excel(RAW_DATA_PATH, engine="openpyxl")
    print(f"Loaded {len(df_raw):,} raw transaction rows in {time.time() - start_t:.1f}s")
    
    # Standardize column types & drop missing CustomerID
    df_valid = df_raw.dropna(subset=["CustomerID"]).copy()
    df_valid["CustomerID"] = df_valid["CustomerID"].astype(int)
    df_valid["InvoiceNo"] = df_valid["InvoiceNo"].astype(str).str.strip()
    df_valid["StockCode"] = df_valid["StockCode"].astype(str).str.strip()
    df_valid["InvoiceDate"] = pd.to_datetime(df_valid["InvoiceDate"])
    
    # Exclude non-merchandise administrative codes
    service_codes = {"POST", "D", "M", "PADS", "DOT", "CRUK", "BANK CHARGES"}
    df_valid = df_valid[~df_valid["StockCode"].isin(service_codes)].copy()
    
    # Segregate Purchases vs Returns
    is_return = df_valid["InvoiceNo"].str.startswith("C") | (df_valid["Quantity"] < 0)
    purchases = df_valid[~is_return & (df_valid["UnitPrice"] > 0) & (df_valid["Quantity"] > 0)].copy()
    returns = df_valid[is_return & (df_valid["UnitPrice"] > 0)].copy()
    
    # Deduplicate exact duplicate rows
    purchases = purchases.drop_duplicates(subset=["InvoiceNo", "StockCode", "Description", "Quantity", "InvoiceDate", "UnitPrice", "CustomerID"])
    returns = returns.drop_duplicates(subset=["InvoiceNo", "StockCode", "Description", "Quantity", "InvoiceDate", "UnitPrice", "CustomerID"])
    
    # Calculate amounts
    purchases["TotalAmount"] = (purchases["Quantity"] * purchases["UnitPrice"]).round(2)
    returns["ReturnAmount"] = (returns["Quantity"] * returns["UnitPrice"]).round(2)
    
    # Temporal Matching for Returns
    min_purchase_date = purchases.groupby(["CustomerID", "StockCode"])["InvoiceDate"].min()
    returns["MinPurchaseDate"] = returns.set_index(["CustomerID", "StockCode"]).index.map(min_purchase_date)
    returns["IsMatched"] = returns["MinPurchaseDate"].notna() & (returns["MinPurchaseDate"] <= returns["InvoiceDate"])
    returns = returns.drop(columns=["MinPurchaseDate"])
    
    # Cohort Month Indexing on Purchases
    first_purchase = purchases.groupby("CustomerID")["InvoiceDate"].min().dt.to_period("M")
    purchases["InvoiceMonth"] = purchases["InvoiceDate"].dt.to_period("M").astype(str)
    purchases["CohortMonth"] = purchases["CustomerID"].map(first_purchase).astype(str)
    
    # Assertions
    assert len(purchases) == 391152, f"Expected 391,152 purchases, got {len(purchases)}"
    assert purchases["CustomerID"].nunique() == 4334, "Expected 4,334 customers"
    assert round(purchases["TotalAmount"].sum(), 2) == 8568758.04, "Gross amount mismatch"
    assert len(returns) == 8505, "Expected 8,505 returns"
    assert returns["IsMatched"].sum() == 7060, "Expected 7,060 matched return rows"
    
    # Save clean datasets
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    purchases.to_parquet(PROCESSED_DIR / "online_retail_clean.parquet", index=False)
    purchases.to_csv(PROCESSED_DIR / "online_retail_clean.csv", index=False)
    returns.to_parquet(PROCESSED_DIR / "returns_clean.parquet", index=False)
    returns.to_csv(PROCESSED_DIR / "returns_clean.csv", index=False)
    
    print(f"Data Cleaning Completed: 391,152 purchases, 8,505 returns saved.")
    return purchases, returns

def run_rfm_pipeline(purchases, returns):
    print("\n" + "=" * 60)
    print("2. RUNNING RFM SEGMENTATION & 25-CELL MAPPING")
    print("=" * 60)
    
    # Identify Zero Net Spend Customers
    cust_gross = purchases.groupby("CustomerID")["TotalAmount"].sum()
    matched_returns = returns[returns["IsMatched"]].groupby("CustomerID")["ReturnAmount"].sum().abs()
    cust_matched_net = cust_gross.subtract(matched_returns, fill_value=0)
    
    zero_net_cids = cust_matched_net[cust_matched_net.abs() < 0.005].index.tolist()
    assert len(zero_net_cids) == 10, f"Expected 8 zero net customers, got {len(zero_net_cids)}"
    print(f"Identified {len(zero_net_cids)} Zero Net Spend customers: {zero_net_cids}")
    
    # Active Customer RFM Calculation
    df_active = purchases[~purchases["CustomerID"].isin(zero_net_cids)].copy()
    rfm = df_active.groupby("CustomerID").agg(
        Recency=("InvoiceDate", lambda x: (REF_DATE - x.max()).days),
        Frequency=("InvoiceNo", "nunique"),
        GrossSpend=("TotalAmount", "sum")
    ).reset_index()
    
    ret_matched = returns[returns["IsMatched"]].groupby("CustomerID")["ReturnAmount"].sum().abs().reset_index()
    ret_matched.columns = ["CustomerID", "MatchedReturnSpend"]
    rfm = rfm.merge(ret_matched, on="CustomerID", how="left")
    rfm["MatchedReturnSpend"] = rfm["MatchedReturnSpend"].fillna(0.0)
    rfm["Monetary"] = rfm["GrossSpend"] - rfm["MatchedReturnSpend"]
    
    assert len(rfm) == 4324, f"Expected 4,326 active customers, got {len(rfm)}"
    assert (rfm["Monetary"] >= 0.01).all(), "All active customers must have Monetary > 0"
    
    # Scoring
    rfm["R_Score"] = pd.cut(rfm["Recency"], bins=[-np.inf, 14, 32, 71, 179, np.inf], labels=[5, 4, 3, 2, 1]).astype(int)
    rfm["F_Score"] = pd.cut(rfm["Frequency"], bins=[0, 1, 2, 3, 5, np.inf], labels=[1, 2, 3, 4, 5]).astype(int)
    rfm["M_Score"] = pd.qcut(rfm["Monetary"], q=5, labels=[1, 2, 3, 4, 5]).astype(int)
    rfm["RFM_Code"] = rfm["R_Score"].astype(str) + rfm["F_Score"].astype(str) + rfm["M_Score"].astype(str)
    
    # 25-Cell Mapping
    segment_map = {
        (5, 5): "Champions", (5, 4): "Champions", (4, 5): "Champions", (4, 4): "Champions",
        (5, 3): "Loyal Customers", (4, 3): "Loyal Customers", (3, 5): "Loyal Customers", (3, 4): "Loyal Customers",
        (5, 2): "Potential Loyalists", (4, 2): "Potential Loyalists", (3, 3): "Potential Loyalists",
        (5, 1): "New Customers", (4, 1): "New Customers",
        (3, 1): "Promising", (3, 2): "Promising",
        (2, 5): "Need Attention", (2, 4): "Need Attention", (2, 3): "Need Attention",
        (2, 2): "About to Sleep", (2, 1): "About to Sleep",
        (1, 5): "At Risk", (1, 4): "At Risk", (1, 3): "At Risk",
        (1, 2): "Hibernating",
        (1, 1): "Lost"
    }
    rfm["Segment"] = rfm.apply(lambda row: segment_map[(row["R_Score"], row["F_Score"])], axis=1)
    
    # Build Zero Net Table with real Recency & Frequency
    zero_recencies = [(REF_DATE - purchases[purchases["CustomerID"] == cid]["InvoiceDate"].max()).days for cid in zero_net_cids]
    zero_frequencies = [purchases[purchases["CustomerID"] == cid]["InvoiceNo"].nunique() for cid in zero_net_cids]
    zero_gross = [purchases[purchases["CustomerID"] == cid]["TotalAmount"].sum() for cid in zero_net_cids]
    zero_returns = [abs(returns[(returns["CustomerID"] == cid) & (returns["IsMatched"])]["ReturnAmount"].sum()) for cid in zero_net_cids]
    
    zero_r_scores = pd.cut(zero_recencies, bins=[-np.inf, 14, 32, 71, 179, np.inf], labels=[5, 4, 3, 2, 1]).astype(int)
    zero_f_scores = pd.cut(zero_frequencies, bins=[0, 1, 2, 3, 5, np.inf], labels=[1, 2, 3, 4, 5]).astype(int)

    df_zero_export = pd.DataFrame({
        "CustomerID": zero_net_cids,
        "Recency": zero_recencies,
        "Frequency": zero_frequencies,
        "GrossSpend": zero_gross,
        "MatchedReturnSpend": zero_returns,
        "Monetary": [0.0] * len(zero_net_cids),
        "R_Score": zero_r_scores,
        "F_Score": zero_f_scores,
        "M_Score": [0] * len(zero_net_cids),
        "RFM_Code": [None] * len(zero_net_cids),
        "Segment": ["Zero Net Spend"] * len(zero_net_cids),
        "IsZeroNet": [True] * len(zero_net_cids)
    })
    
    rfm["IsZeroNet"] = False
    rfm_final = pd.concat([rfm, df_zero_export], ignore_index=True)
    assert len(rfm_final) == 4334, "Expected 4,334 total segmented customers"
    
    # Save files
    rfm_final.to_parquet(PROCESSED_DIR / "rfm_segmented.parquet", index=False)
    rfm_final.to_csv(PROCESSED_DIR / "rfm_segmented.csv", index=False)
    
    print("RFM Segmentation Completed: 4,334 customers classified across 10 segments + Zero Net.")
    print("\nSegment Distribution Summary:")
    print(rfm_final["Segment"].value_counts())

def main():
    total_start = time.time()
    print("STARTING CUSTOMER RETENTION & RFM PRODUCTION PIPELINE")
    
    # If clean files already exist, read them; otherwise clean from raw
    if (PROCESSED_DIR / "online_retail_clean.parquet").exists() and (PROCESSED_DIR / "returns_clean.parquet").exists():
        print("Existing cleaned parquet files found. Reading clean datasets...")
        purchases = pd.read_parquet(PROCESSED_DIR / "online_retail_clean.parquet")
        returns = pd.read_parquet(PROCESSED_DIR / "returns_clean.parquet")
    else:
        purchases, returns = run_cleaning_pipeline()
        
    run_rfm_pipeline(purchases, returns)
    
    print("\n" + "=" * 60)
    print(f"PIPELINE EXECUTED SUCCESSFULLY IN {time.time() - total_start:.2f}s!")
    print(f"Output files stored in: {PROCESSED_DIR}")
    print("=" * 60)

if __name__ == "__main__":
    main()

