"""The 12 demo sources and their approved mappings.

These are hand-written on purpose. They are what a finance executive would approve after the
mapping wizard, and they are the gold standard the AI mapping suggestions are measured against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ingest.mapping import MappingSpec


@dataclass(frozen=True)
class SourceDef:
    partner: str  # partners.code
    kind: str  # sources.kind
    name: str
    filename: str  # file name in the sample data folder
    mapping: dict[str, Any]

    def spec(self) -> MappingSpec:
        return MappingSpec.model_validate(self.mapping)


def _emit(amount: str, line_type: str, sign: int = 1, skip_zero: bool = False) -> dict[str, Any]:
    return {"amount": amount, "line_type": line_type, "sign": sign, "skip_zero": skip_zero}


def _ledger(fmt: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"kind": "ledger", "format": fmt, "ledger": fields}


_CSV = {"kind": "csv"}

SOURCES: tuple[SourceDef, ...] = (
    SourceDef("WEB", "orders", "OMS orders export", "orders.csv", {
        "kind": "orders", "format": _CSV,
        "orders": {
            "order_ref": "Order ID", "channel": "Channel",
            "ordered_on": {"column": "Order Date", "format": "iso"},
            "payment_mode": "Payment Mode", "value": "Order Value", "pincode": "Pincode",
            "weight_g": "Weight (g)", "length_cm": "Length (cm)", "width_cm": "Width (cm)",
            "height_cm": "Height (cm)", "courier": "Courier", "awb": "AWB",
            "delivery_status": "Delivery Status",
            "delivered_on": {"column": "Delivered On", "format": "iso"},
            "returned_on": {"column": "Returned On", "format": "iso"},
            "extras": {"sku": "SKU", "city": "City"},
        }}),
    SourceDef("M1", "settlement", "Settlement report (xlsx)", "settlement_m1.xlsx", _ledger(
        {"kind": "xlsx", "footer_markers": ["Total"]},
        order_ref="Order ID", reference="UTR", extras={"sku": "SKU"},
        date={"column": "Settlement Date", "format": "dmy"},
        emitters=[{
            "amount": "Amount (INR)",
            "line_type_from": ["Transaction Type"],
            "line_type_map": {
                "Item Price": "sale", "Refund": "refund", "Commission": "commission",
                "Fixed Closing Fee": "fixed_fee", "Shipping Fee": "shipping_fee",
                "Return Shipping Fee": "return_fee", "TCS": "tcs", "TDS": "tds"}}])),
    SourceDef("M2", "settlement", "Settlement report", "settlement_m2.csv", _ledger(
        _CSV, order_ref="order_id", reference="utr",
        extras={"city": "city", "remarks": "remarks"},
        date={"column": "settled_on", "format": "dmony"},
        emitters=[
            _emit("gross_sale", "sale"),
            _emit("commission", "commission", -1, True), _emit("fixed_fee", "fixed_fee", -1, True),
            _emit("shipping_fee", "shipping_fee", -1, True), _emit("tcs", "tcs", -1, True),
            _emit("tds", "tds", -1, True), _emit("refund", "refund", -1, True),
            _emit("return_fee", "return_fee", -1, True)],
        net_check={"column": "net_payout"})),
    SourceDef("M3", "settlement", "Settlement report", "settlement_m3.csv", _ledger(
        _CSV, order_ref="order-id", reference="settlement-id",
        date={"column": "posted-date", "format": "iso"},
        emitters=[{
            "amount": "amount",
            "line_type_from": ["amount-type", "amount-description"],
            "line_type_map": {
                "ItemPrice|Principal": "sale", "Refund|Principal": "refund",
                "ItemFees|Commission": "commission", "ItemFees|FixedClosingFee": "fixed_fee",
                "ItemFees|ShippingChargeback": "shipping_fee",
                "ItemFees|ReturnShippingFee": "return_fee",
                "ItemWithheldTax|TCS": "tcs", "ItemWithheldTax|TDS": "tds"}}])),
    SourceDef("Q1", "settlement", "Payout report", "settlement_q1.csv", _ledger(
        _CSV, order_ref="Order No", date={"column": "Payout Date", "format": "dmy"},
        extras={"store_city": "Store City"},
        emitters=[_emit("Sale Value", "sale"), _emit("Platform Fee", "commission", -1),
                  _emit("TCS", "tcs", -1), _emit("TDS", "tds", -1)],
        net_check={"column": "Net Payout"})),
    SourceDef("Q2", "settlement", "Payout report", "settlement_q2.csv", _ledger(
        _CSV, order_ref="Ref", date={"column": "Date", "format": "iso"},
        emitters=[_emit("Order Amount", "sale"), _emit("Commission", "commission"),
                  _emit("TCS", "tcs"), _emit("TDS", "tds")],
        net_check={"column": "Settled Amount"})),
    SourceDef("WEB", "settlement", "Payment gateway settlement", "settlement_web_pg.csv", _ledger(
        _CSV, order_ref="Order ID", date={"column": "Settled On", "format": "iso"},
        extras={"narration": "Narration"},
        emitters=[
            {"amount": "Gross (INR)", "line_type_from": ["Type"],
             "line_type_map": {"settlement": "sale", "refund": "refund"}},
            _emit("Fee (INR)", "pg_fee", -1)],
        net_check={"column": "Net (INR)"})),
    SourceDef("CA", "cod_remittance", "COD remittance", "cod_remittance_ca.csv", _ledger(
        _CSV, order_ref="Order Ref", awb="AWB", reference="UTR",
        date={"column": "Remitted On", "format": "iso"},
        emitters=[_emit("COD Collected", "sale"), _emit("Freight Charged", "freight", -1),
                  _emit("COD Fee", "cod_fee", -1)],
        net_check={"column": "Net Remitted"})),
    SourceDef("CB", "cod_remittance", "COD remittance", "cod_remittance_cb.csv", _ledger(
        _CSV, order_ref="Client Order No", awb="Waybill", reference="Ref No",
        date={"column": "Payout Date", "format": "dmy"},
        emitters=[_emit("Cod Amount", "sale"), _emit("Shipping", "freight", -1),
                  _emit("Cod Charges", "cod_fee", -1)],
        net_check={"column": "Remitted"})),
    SourceDef("CA", "freight_invoice", "Freight invoice", "freight_invoice_ca.csv", _ledger(
        _CSV, order_ref="Order Ref", awb="AWB", reference="Invoice No",
        date={"column": "Invoice Date", "format": "iso"},
        emitters=[_emit("Freight", "freight", -1, True),
                  _emit("Return Freight", "return_freight", -1, True)],
        net_check={"column": "Total", "sign": -1})),
    SourceDef("CB", "freight_invoice", "Freight invoice", "freight_invoice_cb.csv", _ledger(
        _CSV, order_ref="Client Order No", awb="Waybill", reference="Bill No",
        date={"column": "Bill Date", "format": "dmy"},
        emitters=[_emit("Forward Charge", "freight", -1, True),
                  _emit("RTO/Return Charge", "return_freight", -1, True)],
        net_check={"column": "Amount", "sign": -1})),
    SourceDef("WEB", "warehouse_receipt", "Warehouse return scans", "warehouse_receipts.csv", {
        "kind": "receipts", "format": _CSV,
        "receipts": {"order_ref": "Return Order ID",
                     "received_on": {"column": "Received On", "format": "iso"},
                     "condition": "Condition"}}),
)

# Orders must load first so ledger lines can be linked to them as they arrive.
_KIND_RANK = {"orders": 0, "warehouse_receipt": 2}
LOAD_ORDER = tuple(sorted(SOURCES, key=lambda s: _KIND_RANK.get(s.kind, 1)))