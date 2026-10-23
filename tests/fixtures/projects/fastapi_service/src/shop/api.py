"""Tiny shop API used as an engine fixture."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="shop")
ORDERS: dict[int, dict] = {}


class Order(BaseModel):
    item: str = Field(min_length=1, max_length=100)
    quantity: int = Field(ge=1, le=100)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/orders")
def create(order: Order) -> dict:
    order_id = len(ORDERS) + 1
    ORDERS[order_id] = {"id": order_id, **order.model_dump()}
    return ORDERS[order_id]


@app.get("/orders/{order_id}")
def read(order_id: int) -> dict:
    if order_id not in ORDERS:
        raise HTTPException(404, "order not found")
    return ORDERS[order_id]
