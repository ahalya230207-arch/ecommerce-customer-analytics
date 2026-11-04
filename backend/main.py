
import os
from io import BytesIO

import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text

app = FastAPI(title="E-Commerce Analytics API")

# PostgreSQL URL from Supabase.
# SQLite is used only as a fallback for local testing.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ecommerce.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://", "postgresql+psycopg://", 1
    )
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://", "postgresql+psycopg://", 1
    )

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

REQUIRED = [
    "InvoiceNo", "StockCode", "Description", "Quantity",
    "InvoiceDate", "UnitPrice", "CustomerID", "Country"
]


@app.get("/")
def home():
    return {
        "status": "running",
        "message": "E-Commerce Analytics Backend Connected"
    }


@app.get("/api/health")
def health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "healthy", "database": "connected"}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.post("/api/upload")
async def upload_dataset(file: UploadFile = File(...)):
    filename = (file.filename or "").lower()

    if not filename.endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Please upload an Excel .xlsx or .xls file."
        )

    try:
        content = await file.read()

        if len(content) > 30 * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail="File exceeds the 30 MB upload limit."
            )

        df = pd.read_excel(BytesIO(content), engine="openpyxl")
        df.columns = df.columns.astype(str).str.strip()

        missing = [column for column in REQUIRED if column not in df.columns]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required columns: {missing}"
            )

        original_count = len(df)

        df["InvoiceNo"] = df["InvoiceNo"].astype(str).str.strip()
        df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce")
        df["UnitPrice"] = pd.to_numeric(df["UnitPrice"], errors="coerce")
        df["CustomerID"] = df["CustomerID"].astype("string").str.strip()
        df["InvoiceDate"] = pd.to_datetime(
            df["InvoiceDate"], errors="coerce"
        )
        df["Description"] = df["Description"].fillna("Unknown").astype(str)
        df["Country"] = df["Country"].fillna("Unknown").astype(str)

        df = df[
            ~df["InvoiceNo"].str.upper().str.startswith("C")
            & (df["Quantity"] > 0)
            & (df["UnitPrice"] > 0)
            & df["Quantity"].notna()
            & df["UnitPrice"].notna()
            & df["CustomerID"].notna()
            & (df["CustomerID"] != "")
            & df["InvoiceDate"].notna()
        ].copy()

        if df.empty:
            raise HTTPException(
                status_code=400,
                detail="No valid records found after cleaning."
            )

        df["Sales"] = df["Quantity"] * df["UnitPrice"]

        # Store cleaned data in PostgreSQL.
        # Each new upload replaces the previous analysis dataset.
        columns = REQUIRED + ["Sales"]
        df[columns].to_sql(
            "sales",
            engine,
            if_exists="replace",
            index=False,
            chunksize=5000
        )

        return {
            "message": "Dataset uploaded, cleaned and saved successfully!",
            "original_records": original_count,
            "cleaned_records": len(df),
            "removed_records": original_count - len(df)
        }

    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/api/dashboard")
def dashboard():
    try:
        with engine.connect() as connection:
            df = pd.read_sql(text("SELECT * FROM sales"), connection)

        if df.empty:
            raise HTTPException(
                status_code=404,
                detail="No dataset uploaded yet."
            )

        df["Sales"] = pd.to_numeric(df["Sales"], errors="coerce")
        df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce")
        df["InvoiceDate"] = pd.to_datetime(
            df["InvoiceDate"], errors="coerce"
        )

        revenue = float(df["Sales"].sum())
        orders = int(df["InvoiceNo"].nunique())
        customers = int(df["CustomerID"].nunique())
        products = int(df["StockCode"].nunique())

        monthly = (
            df.assign(Month=df["InvoiceDate"].dt.strftime("%Y-%m"))
            .groupby("Month")["Sales"].sum()
            .sort_index()
        )

        top_products = (
            df.groupby("Description")
            .agg(
                quantity=("Quantity", "sum"),
                revenue=("Sales", "sum")
            )
            .sort_values("revenue", ascending=False)
            .head(10)
            .reset_index()
        )

        country = (
            df.groupby("Country")
            .agg(
                orders=("InvoiceNo", "nunique"),
                revenue=("Sales", "sum")
            )
            .sort_values("revenue", ascending=False)
            .head(10)
            .reset_index()
        )

        spending = df.groupby("CustomerID")["Sales"].sum()

        categories = {
            "Below £100": int((spending < 100).sum()),
            "£100–£500": int(
                ((spending >= 100) & (spending < 500)).sum()
            ),
            "£500–£1000": int(
                ((spending >= 500) & (spending < 1000)).sum()
            ),
            "Above £1000": int((spending >= 1000).sum())
        }

        return {
            "total_revenue": round(revenue, 2),
            "total_orders": orders,
            "unique_customers": customers,
            "unique_products": products,
            "cleaned_records": len(df),
            "monthly_sales": [
                {"month": str(k), "revenue": round(float(v), 2)}
                for k, v in monthly.items()
            ],
            "top_products": [
                {
                    "product": str(r.Description),
                    "quantity": int(r.quantity),
                    "revenue": round(float(r.revenue), 2)
                }
                for r in top_products.itertuples()
            ],
            "country_sales": [
                {
                    "country": str(r.Country),
                    "orders": int(r.orders),
                    "revenue": round(float(r.revenue), 2)
                }
                for r in country.itertuples()
            ],
            "customer_distribution": [
                {"category": k, "count": v}
                for k, v in categories.items()
            ]
        }

    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))
