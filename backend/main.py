
import os
from io import BytesIO
from functools import lru_cache

import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text

app = FastAPI(title="E-Commerce Analytics API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL")


@lru_cache(maxsize=1)
def get_engine():
    if not DATABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="Database not configured. Add DATABASE_URL in Render."
        )

    url = DATABASE_URL

    if url.startswith("postgres://"):
        url = url.replace(
            "postgres://", "postgresql+psycopg://", 1
        )
    elif url.startswith("postgresql://"):
        url = url.replace(
            "postgresql://", "postgresql+psycopg://", 1
        )

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0
    )


@app.get("/")
def home():
    return {
        "status": "running",
        "message": "E-Commerce Analytics Backend Connected"
    }


@app.get("/api/health")
def database_health():
    try:
        with get_engine().connect() as connection:
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
        del contents

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

        missing = [
            column for column in required
            if column not in df.columns
        ]

        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing Excel columns: {missing}"
            )

        df["InvoiceNo"] = df["InvoiceNo"].astype(str).str.strip()
        df["Quantity"] = pd.to_numeric(
            df["Quantity"], errors="coerce"
        )
        df["UnitPrice"] = pd.to_numeric(
            df["UnitPrice"], errors="coerce"
        )
        df["InvoiceDate"] = pd.to_datetime(
            df["InvoiceDate"], errors="coerce"
        )

        df = df.dropna(
            subset=[
                "InvoiceNo",
                "StockCode",
                "Description",
                "Quantity",
                "InvoiceDate",
                "UnitPrice",
                "Country"
            ]
        )

        df = df[
            ~df["InvoiceNo"].str.startswith("C", na=False)
        ]

        df = df[
            (df["Quantity"] > 0) &
            (df["UnitPrice"] > 0)
        ]

        df["Sales"] = df["Quantity"] * df["UnitPrice"]

        if df.empty:
            raise HTTPException(
                status_code=400,
                detail="No valid sales records found."
            )

        records_saved = len(df)
        total_revenue = round(float(df["Sales"].sum()), 2)

        engine = get_engine()

        df.to_sql(
            "sales",
            engine,
            if_exists="replace",
            index=False,
            chunksize=500,
            method="multi"
        )

        del df

        return {
            "status": "success",
            "message": "Dataset saved to PostgreSQL database.",
            "database": "PostgreSQL",
            "records_saved": int(records_saved),
            "total_revenue": total_revenue
        }

    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


@app.get("/api/dashboard")
def dashboard():
    try:
        engine = get_engine()

        with engine.connect() as connection:

            # Check whether the sales table exists.
            table_check = connection.execute(
                text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = current_schema()
                        AND table_name = 'sales'
                    )
                """)
            ).scalar()

            if not table_check:
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

            # PostgreSQL calculates all main KPIs.
            totals = connection.execute(
                text("""
                    SELECT
                        COALESCE(SUM("Sales"), 0) AS revenue,
                        COUNT(DISTINCT "InvoiceNo") AS orders,
                        COUNT(DISTINCT "CustomerID") AS customers,
                        COUNT(DISTINCT "StockCode") AS products,
                        COUNT(*) AS rows
                    FROM sales
                """)
            ).mappings().one()

            # Monthly sales.
            monthly = connection.execute(
                text("""
                    SELECT
                        TO_CHAR(
                            DATE_TRUNC('month', "InvoiceDate"),
                            'YYYY-MM'
                        ) AS month,
                        SUM("Sales") AS sales
                    FROM sales
                    WHERE "InvoiceDate" IS NOT NULL
                    GROUP BY DATE_TRUNC('month', "InvoiceDate")
                    ORDER BY DATE_TRUNC('month', "InvoiceDate")
                """)
            ).mappings().all()

            # Top 10 products.
            products = connection.execute(
                text("""
                    SELECT
                        "Description" AS product,
                        SUM("Sales") AS sales
                    FROM sales
                    GROUP BY "Description"
                    ORDER BY sales DESC
                    LIMIT 10
                """)
            ).mappings().all()

            # Top 10 countries.
            countries = connection.execute(
                text("""
                    SELECT
                        "Country" AS country,
                        SUM("Sales") AS sales
                    FROM sales
                    GROUP BY "Country"
                    ORDER BY sales DESC
                    LIMIT 10
                """)
            ).mappings().all()

            # Top 10 customers by spending.
            customers = connection.execute(
                text("""
                    SELECT
                        CAST("CustomerID" AS TEXT) AS customer,
                        SUM("Sales") AS spend
                    FROM sales
                    WHERE "CustomerID" IS NOT NULL
                    GROUP BY "CustomerID"
                    ORDER BY spend DESC
                    LIMIT 10
                """)
            ).mappings().all()

        # Convert PostgreSQL Decimal values into JSON numbers.
        return {
            "revenue": round(float(totals["revenue"]), 2),
            "orders": int(totals["orders"]),
            "customers": int(totals["customers"]),
            "products": int(totals["products"]),
            "rows": int(totals["rows"]),

            "monthly_sales": [
                {
                    "month": item["month"],
                    "sales": round(float(item["sales"]), 2)
                }
                for item in monthly
            ],

            "top_products": [
                {
                    "product": item["product"],
                    "sales": round(float(item["sales"]), 2)
                }
                for item in products
            ],

            "country_sales": [
                {
                    "country": item["country"],
                    "sales": round(float(item["sales"]), 2)
                }
                for item in countries
            ],

            "customer_spend": [
                {
                    "customer": item["customer"],
                    "spend": round(float(item["spend"]), 2)
                }
                for item in customers
            ]
        }

    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=str(error)
        )
