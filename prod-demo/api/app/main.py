from fastapi import FastAPI
from pydantic import BaseModel, Field

from forge_demo_lib import calculate_total

app = FastAPI(title="Forge Production Demo API")


class QuoteRequest(BaseModel):
    subtotal: float = Field(gt=0)
    tax_rate: float = Field(ge=0, le=1)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/quote")
def quote(request: QuoteRequest) -> dict[str, str | float]:
    return {
        "subtotal": request.subtotal,
        "tax_rate": request.tax_rate,
        "total": calculate_total(request.subtotal, request.tax_rate),
    }
