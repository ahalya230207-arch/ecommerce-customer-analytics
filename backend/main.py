import os
from io import BytesIO

import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text, inspect

app = FastAPI(title="E-Commerce Analytics API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database URL is configured securely in Render Environment.
DATABASE_URL = os.getenv("DATABASE_URL")


def get_engine():
    if not DATABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="Database not configured. Add DATABASE_URL in Render."
        )

    url = DATABASE_URL

    # Use psycopg 3 with SQLAlchemy.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(url, pool_pre_ping=True)


@app.get("/")
def home():
    return {
        "status": "running",
        "message": "E-Commerce Analytics Backend Connected"
    }


@app.get("/api/health")
def database_health():
    try:
        engine = get_engine()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        return {
            "backend": "connected",
            "database": "connected",
            "database_type": "PostgreSQL"
        }

    except Exception as error:
        return {
            "backend": "running",
            "database": "not connected",
            "error": str(error)
        }


@app.post("/api/upload")
async def upload_dataset(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Please upload an Excel file."
        )

    try:
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents))

        # Clean column names.
        df.columns = df.columns.astype(str).str.strip()

        required = [
            "InvoiceNo",
            "StockCode",
            "Description",
            "Quantity",
            "InvoiceDate",
            "UnitPrice",
            "CustomerID",
            "Country"
        ]

        missing = [column for column in required if column not in df.columns]

        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing Excel columns: {missing}"
            )

        # Clean data.
        df["InvoiceNo"] = df["InvoiceNo"].astype(str).str.strip()
        df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce")
        df["UnitPrice"] = pd.to_numeric(df["UnitPrice"], errors="coerce")
        df["InvoiceDate"] = pd.to_datetime(
            df["InvoiceDate"], errors="coerce"
        )

        df = df.dropna(
            subset=["InvoiceNo", "StockCode", "Description",
                    "Quantity", "InvoiceDate", "UnitPrice", "Country"]
        )

        # Remove cancelled invoices and invalid transactions.
        df = df[~df["InvoiceNo"].str.startswith("C", na=False)]
        df = df[(df["Quantity"] > 0) & (df["UnitPrice"] > 0)]

        df["Sales"] = df["Quantity"] * df["UnitPrice"]

        if df.empty:
            raise HTTPException(
                status_code=400,
                detail="No valid sales records found."
            )

        # Store cleaned records in PostgreSQL.
        engine = get_engine()

        df.to_sql(
            "sales",
            engine,
            if_exists="replace",
            index=False,
            chunksize=1000
        )

        return {
            "status": "success",
            "message": "Dataset saved to PostgreSQL database.",
            "database": "PostgreSQL",
            "records_saved": int(len(df)),
            "total_revenue": round(float(df["Sales"].sum()), 2)
        }

    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/api/dashboard")
def dashboard():
    try:
        engine = get_engine()

        if not inspect(engine).has_table("sales"):
            return {
                "revenue": 0,
                "orders": 0,
                "customers": 0,
                "products": 0,
                "rows": 0,
                "monthly_sales": [],
                "top_products": [],
                "country_sales": [],
                "customer_spend": []
            }

        with engine.connect() as connection:
            df = pd.read_sql_query(
                text('SELECT * FROM "sales"'),
                connection
            )

        if df.empty:
            return {
                "revenue": 0,
                "orders": 0,
                "customers": 0,
                "products": 0,
                "rows": 0,
                "monthly_sales": [],
                "top_products": [],
                "country_sales": [],
                "customer_spend": []
            }

        df["InvoiceDate"] = pd.to_datetime(
            df["InvoiceDate"], errors="coerce"
        )

        revenue = float(df["Sales"].sum())

        monthly = (
            df.dropna(subset=["InvoiceDate"])
            .assign(month=lambda x: x["InvoiceDate"].dt.strftime("%Y-%m"))
            .groupby("month", as_index=False)["Sales"]
            .sum()
        )

        top_products = (
            df.groupby("Description", as_index=False)["Sales"]
            .sum()
            .nlargest(10, "Sales")
            .rename(columns={
                "Description": "product",
                "Sales": "sales"
            })
        )

        country_sales = (
            df.groupby("Country", as_index=False)["Sales"]
            .sum()
            .nlargest(10, "Sales")
            .rename(columns={
                "Country": "country",
                "Sales": "sales"
            })
        )

        customer_spend = (
            df.dropna(subset=["CustomerID"])
            .groupby("CustomerID", as_index=False)["Sales"]
            .sum()
            .nlargest(10, "Sales")
        )

        customer_spend["customer"] = (
            customer_spend["CustomerID"].astype(str)
        )
        customer_spend = customer_spend.rename(
            columns={"Sales": "spend"}
        )

        return {
            "revenue": round(revenue, 2),
            "orders": int(df["InvoiceNo"].nunique()),
            "customers": int(df["CustomerID"].nunique()),
            "products": int(df["StockCode"].nunique()),
            "rows": int(len(df)),
            "monthly_sales": monthly.rename(
                columns={"Sales": "sales"}
            ).to_dict(orient="records"),
            "top_products": top_products.to_dict(orient="records"),
            "country_sales": country_sales.to_dict(orient="records"),
            "customer_spend": customer_spend[
                ["customer", "spend"]
            ].to_dict(orient="records")
        }

    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))
        
