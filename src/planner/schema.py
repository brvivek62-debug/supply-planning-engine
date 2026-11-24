"""Single source of truth for every table.

Each table declares its fields (dtype, required, allowed values, unit, unique, validation rule,
description), primary key, natural key and foreign keys. The data dictionary, ``sql/schema.sql``,
the ERD, the writers (column order, rounding) and the validators are all derived from here.

Tables are listed in dependency order: a table only references tables declared before it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------------------------
# Shared enumerations
# --------------------------------------------------------------------------------------------

LOCATION_TYPES = ("PLANT", "DC", "STORE")
REGIONS = ("NORTH", "SOUTH", "EAST", "WEST", "CENTRAL")
CATEGORIES = (
    "BEVERAGES", "SNACKS", "DAIRY", "STAPLES",
    "PERSONAL_CARE", "HOME_CARE", "BABY_CARE", "HOUSEHOLD_ELECTRICALS",
)
SKU_TYPES = ("FINISHED", "COMPONENT")
SOURCING_TYPES = ("BUY", "MAKE")
UOMS = ("EA", "CASE")
CASE_PACKS = (1, 6, 12, 24)
ABC = ("A", "B", "C")
CRITICALITY = ("HIGH", "MEDIUM", "LOW")
SKU_LIFECYCLE = ("NEW", "ACTIVE", "PHASE_OUT")
SUPPLIER_STATUS = ("ACTIVE", "ON_HOLD", "INACTIVE")
APPROVAL = ("APPROVED", "PENDING", "REJECTED")
TRANSPORT_MODES = ("ROAD", "RAIL", "SEA", "AIR")
DEMAND_DRIVERS = ("BASELINE", "SEASONAL_PEAK", "PROMO")
TXN_TYPES = ("OPENING", "RECEIPT", "ISSUE", "ADJUSTMENT")
INVENTORY_STATUS = ("ON_HAND", "IN_TRANSIT")
PO_STATUS = ("CREATED", "CONFIRMED", "SHIPPED", "IN_TRANSIT", "DELAYED", "RECEIVED", "CANCELLED")
PO_OPEN_STATUS = ("CREATED", "CONFIRMED", "SHIPPED", "IN_TRANSIT", "DELAYED")
PO_ACTOR_ROLES = ("PLANNER", "SUPPLIER", "CARRIER", "WAREHOUSE_OPERATOR", "LOGISTICS_ANALYST")
PO_REASON_CODES = (
    "PO_CREATED", "SUPPLIER_CONFIRMED", "DISPATCHED", "HANDED_TO_CARRIER", "TRANSPORT_DELAY",
    "DELAY_CLEARED", "GOODS_RECEIVED", "DEMAND_CHANGE", "SUPPLIER_REJECTED",
)
PRODUCTION_STATUS = ("FIRM", "PLANNED")
BALANCE_STATUS = ("OK", "BELOW_SS", "STOCKOUT")
SHORTAGE_TYPES = ("STOCKOUT", "BELOW_SAFETY_STOCK")
CAUSE_CODES = (
    "DEMAND_SPIKE", "PO_DELAYED", "SUPPLIER_CAPACITY", "LEAD_TIME_FENCE",
    "LOW_OPENING_STOCK", "COMPONENT_SHORTAGE", "PRODUCTION_CAPACITY",
)
EVIDENCE_TYPES = (
    "forecast_demand", "purchase_order", "planned_order", "supplier_capacity",
    "lead_time", "inventory", "production_plan", "bom",
)
SHORTAGE_STATUS = (
    "OPEN", "MITIGATION_PROPOSED", "MITIGATION_SELECTED", "ESCALATED", "BACKORDER_ACCEPTED",
)
SHORTAGE_ACTOR_ROLES = ("PLANNER", "OPERATIONS_MANAGER")
SHORTAGE_REASON_CODES = (
    "PAB_BELOW_SAFETY_STOCK", "OPTIONS_GENERATED", "BEST_COMPLIANT_OPTION",
    "APPROVAL_REQUIRED", "NO_FEASIBLE_OPTION",
)
MITIGATION_TYPES = (
    "EXPEDITE_PO", "ALTERNATE_SUPPLIER", "TRANSFER", "SUBSTITUTE_SKU",
    "INCREASE_PRODUCTION", "BACKORDER",
)
MITIGATION_SOURCES = ("SYSTEM", "MANUAL")
MITIGATION_CREATORS = ("SYSTEM", "PLANNER")
POLICY_STATUS = ("COMPLIANT", "VIOLATION")
LEDGER_RESOURCES = ("SUPPLIER_CAPACITY", "PLANT_CAPACITY", "STOCK")
ISSUE_TYPES = (
    "DUPLICATE_INBOUND", "UNAPPROVED_SUBSTITUTE", "SUPPLIER_CAPACITY_EXCEEDED",
    "LEAD_TIME_IGNORED", "UOM_MISMATCH",
)
BOUNDARY_TYPES = ("PO_QTY_EQUALS_MOQ", "SUPPLIER_LOAD_EQUALS_CAPACITY", "RECEIPT_EQUALS_LEAD_TIME")
EXCEPTION_ACTIONS = ("EXCLUDED", "CORRECTED", "CAPPED", "FLAGGED")
SEVERITY = ("HIGH", "MEDIUM", "LOW")
CASE_CLASSES = ("ANOMALY", "BOUNDARY")

DTYPES = ("string", "integer", "decimal", "boolean", "date", "datetime", "week")

# Where each table's rows come from.
KIND_INPUT = "input"        # generated source data (data/<scale>/)
KIND_MIXED = "input+output"  # FIRM rows generated, PLANNED rows created by the planner
KIND_OUTPUT = "output"      # derived by the planner, never randomly generated
KIND_LABEL = "label"        # ground truth, written only by inject/, read only by evaluate


# --------------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    name: str
    dtype: str
    description: str
    required: bool = True
    allowed: tuple[Any, ...] | None = None
    unit: str | None = None
    unique: bool = False
    rule: str = ""
    id_kind: str | None = None      # key into ids.REGEX
    ge: float | None = None         # value >= ge
    le: float | None = None         # value <= le
    gt: float | None = None         # value > gt
    decimals: int | None = None     # fixed rounding on write

    def auto_rules(self) -> list[str]:
        out: list[str] = []
        if self.id_kind:
            out.append(f"matches ID pattern `{self.id_kind}`")
        if self.allowed is not None:
            out.append("in allowed values")
        if self.gt is not None:
            out.append(f"> {self.gt:g}")
        if self.ge is not None and self.le is not None:
            out.append(f"between {self.ge:g} and {self.le:g}")
        elif self.ge is not None:
            out.append(f">= {self.ge:g}")
        elif self.le is not None:
            out.append(f"<= {self.le:g}")
        if self.dtype == "week":
            out.append("format `YYYYWnn` (2025-2027)" if self.id_kind == "iso_week" else "format `2026Wnn`")
        if self.dtype in ("date", "datetime"):
            out.append("ISO 8601")
        if self.decimals is not None:
            out.append(f"rounded to {self.decimals} dp")
        return out

    @property
    def full_rule(self) -> str:
        parts = [self.rule] if self.rule else []
        parts += self.auto_rules()
        if not parts:
            parts = ["non-empty" if self.required else "may be empty"]
        return "; ".join(parts)


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]
    optional: bool = False         # FK column may be NULL
    cardinality: str = "1:N"       # parent : child
    label: str = ""


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    kind: str
    group: str
    fields: tuple[Field, ...]
    pk: tuple[str, ...]
    fks: tuple[ForeignKey, ...] = ()
    natural_key: tuple[str, ...] | None = None
    core_entity: str | None = None          # name used in the brief, if it is a core entity
    effective_dated: bool = False
    lifecycle: str | None = None            # key into LIFECYCLES
    notes: tuple[str, ...] = ()
    polymorphic_refs: tuple[str, ...] = ()  # columns that point at several tables (no SQL FK)

    @property
    def columns(self) -> list[str]:
        return [f.name for f in self.fields]

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(f"{self.name}.{name}")

    def fk_for(self, column: str) -> ForeignKey | None:
        for fk in self.fks:
            if fk.columns == (column,):
                return fk
        return None


# --------------------------------------------------------------------------------------------
# Field helpers (keep the table definitions compact and uniform)
# --------------------------------------------------------------------------------------------

def _id(name: str, kind: str, desc: str, *, pk: bool = False, required: bool = True, rule: str = "") -> Field:
    return Field(name, "string", desc, required=required, unique=pk, id_kind=kind, rule=rule)


def _enum(name: str, values: tuple[Any, ...], desc: str, *, required: bool = True, rule: str = "",
          dtype: str = "string") -> Field:
    return Field(name, dtype, desc, required=required, allowed=values, rule=rule)


def _qty(name: str, desc: str, *, required: bool = True, signed: bool = False, rule: str = "",
         unit: str = "EA", gt: float | None = None) -> Field:
    return Field(name, "integer", desc, required=required, unit=unit, rule=rule,
                 ge=None if (signed or gt is not None) else 0, gt=gt)


def _money(name: str, desc: str, *, required: bool = True, rule: str = "", gt: float | None = None,
           signed: bool = False) -> Field:
    return Field(name, "decimal", desc, required=required, unit="INR", rule=rule, decimals=2,
                 gt=gt, ge=None if (signed or gt is not None) else 0)


def _rate(name: str, desc: str, *, required: bool = True, rule: str = "", lo: float = 0, hi: float = 1,
          unit: str = "ratio 0-1") -> Field:
    return Field(name, "decimal", desc, required=required, unit=unit, rule=rule, decimals=4, ge=lo, le=hi)


def _bool(name: str, desc: str, rule: str = "") -> Field:
    return Field(name, "boolean", desc, allowed=(True, False), rule=rule)


def _date(name: str, desc: str, *, required: bool = True, rule: str = "") -> Field:
    return Field(name, "date", desc, required=required, rule=rule)


def _week(name: str, desc: str, *, required: bool = True, rule: str = "", any_year: bool = False) -> Field:
    return Field(name, "week", desc, required=required, unit="ISO week",
                 id_kind="iso_week" if any_year else "week", rule=rule)


def _text(name: str, desc: str, *, required: bool = True, rule: str = "") -> Field:
    return Field(name, "string", desc, required=required, rule=rule)


def _eff_from(desc: str = "Date from which the record is valid.") -> Field:
    return _date("effective_from", desc)


def _eff_to(desc: str = "Last date the record is valid; empty = open-ended.") -> Field:
    return _date("effective_to", desc, required=False, rule="effective_to >= effective_from when present")


def fk(col: str | tuple[str, ...], table: str, ref: str | tuple[str, ...] | None = None, *,
       optional: bool = False, card: str = "1:N", label: str = "") -> ForeignKey:
    cols = (col,) if isinstance(col, str) else tuple(col)
    if ref is None:
        refs = cols
    else:
        refs = (ref,) if isinstance(ref, str) else tuple(ref)
    return ForeignKey(cols, table, refs, optional=optional, cardinality=card, label=label or "references")


# --------------------------------------------------------------------------------------------
# Tables (dependency order)
# --------------------------------------------------------------------------------------------

LOCATION = Table(
    "location", "Plants, distribution centres and stores.", KIND_INPUT, "master",
    core_entity="Location", effective_dated=True,
    pk=("location_id",),
    fields=(
        _id("location_id", "location", "Location identifier LOC-nnn.", pk=True),
        _text("location_name", "Human-readable name, e.g. 'Pune DC 1'."),
        _enum("location_type", LOCATION_TYPES, "Role in the network. PLANT makes MAKE SKUs and stocks components."),
        _text("city", "Indian city (public knowledge, used for realism only)."),
        _enum("region", REGIONS, "Region used for lead-time lanes and transfer distance bands."),
        _bool("is_active", "Location can receive and ship stock."),
        _eff_from(),
    ),
)

SKU = Table(
    "sku", "Stock-keeping units: finished goods and purchased components.", KIND_INPUT, "master",
    core_entity="SKU", effective_dated=True, lifecycle="sku_lifecycle",
    pk=("sku_id",),
    fields=(
        _id("sku_id", "sku", "SKU identifier SKU-nnnnnn.", pk=True),
        _text("sku_name", "Descriptive name."),
        _enum("category", CATEGORIES, "Product category (8). Substitutes must share the category."),
        _enum("sku_type", SKU_TYPES, "FINISHED goods have forecast demand; COMPONENTs only dependent demand."),
        _enum("sourcing_type", SOURCING_TYPES, "BUY from suppliers or MAKE at a plant from a BOM.",
              rule="COMPONENT => BUY"),
        _enum("base_uom", ("EA",), "Base unit of measure; all *_ea quantities use it."),
        Field("case_pack_qty", "integer", "Eaches per case; used as uom_conversion when buying in CASE.",
              allowed=CASE_PACKS, unit="EA/case"),
        _money("unit_cost_inr", "Standard unit cost.", gt=0),
        _money("unit_price_inr", "Selling price used for value at risk and lost margin.", gt=0,
               rule="unit_price_inr > unit_cost_inr"),
        _enum("abc_class", ABC, "Volume class: A ~20% of SKUs and ~75-80% of volume."),
        _enum("criticality", CRITICALITY, "Service criticality; weight 3/2/1 in shortage priority."),
        _enum("lifecycle_state", SKU_LIFECYCLE, "Product lifecycle state."),
        _eff_from("Launch / listing date."),
        _eff_to("Delisting date for PHASE_OUT SKUs; empty otherwise."),
        Field("safety_stock_weeks", "integer", "Target safety stock cover in weeks of mean forecast.",
              unit="weeks", ge=0, le=6),
    ),
)

SUPPLIER = Table(
    "supplier", "Vendors that supply BUY SKUs. Constraint is not stored; it emerges from capacity.",
    KIND_INPUT, "master", core_entity="Supplier", effective_dated=True, lifecycle="supplier_status",
    pk=("supplier_id",),
    fields=(
        _id("supplier_id", "supplier", "Supplier identifier SUP-nnnn.", pk=True),
        _text("supplier_name", "Synthetic company name."),
        _enum("home_region", REGIONS, "Region the supplier ships from."),
        _enum("status", SUPPLIER_STATUS, "Commercial status; only ACTIVE suppliers may receive new orders."),
        _rate("on_time_rate", "Historical on-time delivery rate (Beta distributed, mostly 0.75-0.98)."),
        _eff_from(),
        _eff_to(),
    ),
)

SKU_LOCATION = Table(
    "sku_location", "Assortment bridge (SKU N:M Location): which SKU is stocked where, with safety stock.",
    KIND_INPUT, "bridge", effective_dated=True,
    pk=("sku_id", "location_id"),
    fks=(fk("sku_id", "sku", label="stocked as"), fk("location_id", "location", label="stocks")),
    fields=(
        _id("sku_id", "sku", "Stocked SKU."),
        _id("location_id", "location", "Stocking location.",
            rule="COMPONENT and MAKE SKUs only at PLANT; BUY FINISHED only at DC/STORE"),
        _qty("safety_stock_qty", "Safety stock target.",
             rule="= round(sku.safety_stock_weeks x mean weekly forecast_qty_ea at this location)"),
        _bool("is_active", "Assortment line is active."),
        _eff_from(),
    ),
)

SUPPLIER_SKU = Table(
    "supplier_sku", "Sourcing bridge (Supplier N:M SKU) with commercial terms.",
    KIND_INPUT, "bridge", effective_dated=True,
    pk=("supplier_sku_id",), natural_key=("supplier_id", "sku_id", "effective_from"),
    fks=(fk("supplier_id", "supplier", label="sells"), fk("sku_id", "sku", label="sourced via")),
    fields=(
        _id("supplier_sku_id", "supplier_sku", "Sourcing line identifier SS-nnnnnn.", pk=True),
        _id("supplier_id", "supplier", "Supplier."),
        _id("sku_id", "sku", "BUY SKU.", rule="sku.sourcing_type = BUY"),
        _bool("is_primary", "Primary source.", rule="exactly one primary per BUY SKU"),
        _enum("approval_status", APPROVAL, "Quality / commercial approval. Only APPROVED lines may be ordered."),
        _money("unit_cost_inr", "Purchase price per EA from this supplier.", gt=0),
        _enum("purchase_uom", UOMS, "Unit the supplier sells in."),
        Field("uom_conversion", "integer", "EA per purchase unit.", unit="EA/purchase unit", ge=1,
              rule="= 1 when EA; = sku.case_pack_qty when CASE"),
        Field("moq", "integer", "Minimum order quantity.", unit="purchase unit", ge=1),
        Field("order_multiple", "integer", "Orders must be a multiple of this.", unit="purchase unit", ge=1,
              rule="moq is a multiple of order_multiple"),
        _eff_from(),
        _eff_to(),
    ),
)

SKU_SUBSTITUTE = Table(
    "sku_substitute", "Substitution bridge (SKU N:M SKU): which SKU may replace another.",
    KIND_INPUT, "bridge", effective_dated=True, lifecycle="approval",
    pk=("substitute_id",), natural_key=("sku_id", "substitute_sku_id"),
    fks=(fk("sku_id", "sku", label="can be replaced via"),
         fk("substitute_sku_id", "sku", "sku_id", label="substitutes in")),
    fields=(
        _id("substitute_id", "substitute", "Substitution rule identifier SSUB-nnnnn.", pk=True),
        _id("sku_id", "sku", "Original FINISHED SKU."),
        _id("substitute_sku_id", "sku", "Replacement SKU.",
            rule="!= sku_id; same category; FINISHED"),
        _enum("approval_status", APPROVAL, "Only APPROVED and effective substitutes may be recommended."),
        Field("substitution_ratio", "decimal", "EA of substitute per EA of original.", unit="ratio",
              decimals=4, gt=0, le=2),
        Field("price_delta_pct", "decimal", "Substitute price minus original price, as % of original.",
              unit="%", decimals=4, ge=-50, le=50),
        _eff_from(),
        _eff_to(),
    ),
)

BOM = Table(
    "bom", "Bill of materials bridge (MAKE SKU N:M COMPONENT SKU).",
    KIND_INPUT, "bridge", effective_dated=True,
    pk=("bom_id",), natural_key=("parent_sku_id", "component_sku_id", "effective_from"),
    fks=(fk("parent_sku_id", "sku", "sku_id", label="is made from"),
         fk("component_sku_id", "sku", "sku_id", label="is used in")),
    fields=(
        _id("bom_id", "bom", "BOM line identifier BOM-nnnnn.", pk=True),
        _id("parent_sku_id", "sku", "MAKE FINISHED SKU.", rule="sku.sourcing_type = MAKE"),
        _id("component_sku_id", "sku", "COMPONENT SKU.", rule="sku.sku_type = COMPONENT"),
        Field("qty_per", "integer", "Component EA per parent EA.", unit="EA/EA", ge=1),
        _enum("uom", ("EA",), "Unit of qty_per."),
        _eff_from(),
        _eff_to(),
    ),
)

LEAD_TIME = Table(
    "lead_time", "Supplier-to-region transport lanes, versioned by effective date.",
    KIND_INPUT, "supply", core_entity="LeadTime", effective_dated=True,
    pk=("lead_time_id",), natural_key=("supplier_id", "dest_region", "is_expedite_lane", "effective_from"),
    fks=(fk("supplier_id", "supplier", label="ships via"),),
    notes=("Every supplier has one standard lane and one AIR expedite lane per region.",
           "About 5% of lanes change from 2026W27: two rows, the first ends the day before the second starts.",
           "The planner uses the row effective on the release / order date."),
    fields=(
        _id("lead_time_id", "lead_time", "Lane version identifier LT-nnnnnn.", pk=True),
        _id("supplier_id", "supplier", "Shipping supplier."),
        _enum("dest_region", REGIONS, "Destination region."),
        _enum("transport_mode", TRANSPORT_MODES, "ROAD 1-3 wk, RAIL 2-4, SEA 5-9, AIR 1-2 (expedite)."),
        _bool("is_expedite_lane", "AIR expedite lane (costs an air premium).",
              rule="true <=> transport_mode = AIR"),
        Field("production_days", "integer", "Supplier production / picking time.", unit="days", ge=0),
        Field("transit_days", "integer", "Transport time.", unit="days", ge=0),
        Field("total_lead_weeks", "integer", "Total lead time.", unit="weeks", ge=1,
              rule="= ceil((production_days + transit_days) / 7)"),
        _eff_from(),
        _eff_to(),
    ),
)

SUPPLIER_CAPACITY = Table(
    "supplier_capacity", "Quantity a supplier can deliver per receipt week (all SKUs combined).",
    KIND_INPUT, "supply", core_entity="SupplierCapacity",
    pk=("capacity_id",), natural_key=("supplier_id", "week"),
    fks=(fk("supplier_id", "supplier", label="has"),),
    notes=("Sized as seasonal expected weekly load (level-corrected to the published forecast) x headroom 1.15-1.3; "
           "~10% of suppliers have one temporary 4-8 week disruption at 0.5-0.7x. No disruption flag is stored.",),
    fields=(
        _id("capacity_id", "capacity", "Capacity row identifier CAP-nnnnnnn.", pk=True),
        _id("supplier_id", "supplier", "Supplier."),
        _week("week", "Receipt week the capacity applies to."),
        _qty("capacity_qty_ea", "Deliverable quantity in the week."),
    ),
)

PRODUCTION_CAPACITY = Table(
    "production_capacity", "Plant production capacity per week (1 EA of any MAKE SKU = 1 capacity unit).",
    KIND_INPUT, "supply",
    pk=("plant_location_id", "week"),
    fks=(fk("plant_location_id", "location", "location_id", label="has"),),
    fields=(
        _id("plant_location_id", "location", "Plant.", rule="location.location_type = PLANT"),
        _week("week", "Production week."),
        _qty("capacity_qty_ea", "Units the plant can produce in the week."),
    ),
)

FORECAST_DEMAND = Table(
    "forecast_demand", "Weekly forecast demand per FINISHED SKU-location.",
    KIND_INPUT, "demand", core_entity="ForecastDemand",
    pk=("demand_id",), natural_key=("sku_id", "location_id", "week"),
    fks=(fk("sku_id", "sku", label="forecast for"), fk("location_id", "location", label="forecast at"),
         fk(("sku_id", "location_id"), "sku_location", label="demand of")),
    notes=("Lognormal base rate by ABC class x location-size multiplier x category seasonality, "
           "negative-binomial noise, ~3% PROMO weeks at 2-4x baseline.",
           "COMPONENT demand is dependent demand (production x BOM), not stored here."),
    fields=(
        _id("demand_id", "demand", "DEM-<sku_id>-<location_id>-<week>.", pk=True,
            rule="= 'DEM-' + sku_id + '-' + location_id + '-' + week"),
        _id("sku_id", "sku", "FINISHED SKU.", rule="sku.sku_type = FINISHED"),
        _id("location_id", "location", "Location."),
        _week("week", "Demand week."),
        _date("week_start_date", "Monday of the ISO week.", rule="= date.fromisocalendar(2026, n, 1)"),
        _qty("baseline_qty_ea", "Expected demand without promotions or seasonality."),
        _qty("forecast_qty_ea", "Forecast demand used for planning."),
        _enum("demand_driver", DEMAND_DRIVERS, "Main driver of the week's forecast.",
              rule="PROMO weeks have forecast >= 2x baseline"),
    ),
)

PURCHASE_ORDER = Table(
    "purchase_order", "Inbound purchase orders (firm supply).",
    KIND_INPUT, "orders", core_entity="PurchaseOrder", lifecycle="purchase_order",
    pk=("po_id",),
    fks=(fk("supplier_id", "supplier", label="receives"), fk("sku_id", "sku", label="ordered on"),
         fk("ship_to_location_id", "location", "location_id", label="ships to"),
         fk("supplier_sku_id", "supplier_sku", label="orders under"),
         fk("lead_time_id", "lead_time", label="planned on"),
         fk(("sku_id", "ship_to_location_id"), "sku_location", ("sku_id", "location_id"), label="replenishes")),
    notes=("Status is derived from the last purchase_order_event.",
           "Order dates 2025-09 to 2026-04; ~20% RECEIVED before as-of, ~4% CANCELLED, rest open.",
           "Injected anomalies continue the counter (e.g. PO-2026-010001)."),
    fields=(
        _id("po_id", "purchase_order", "PO-2026-nnnnnn.", pk=True),
        _id("supplier_id", "supplier", "Supplier.", rule="= supplier_sku.supplier_id"),
        _id("sku_id", "sku", "BUY SKU.", rule="= supplier_sku.sku_id"),
        _id("ship_to_location_id", "location", "Destination location."),
        _id("supplier_sku_id", "supplier_sku", "Sourcing line ordered under.",
            rule="APPROVED and effective on order_date"),
        _id("lead_time_id", "lead_time", "Lane used.",
            rule="same supplier; dest_region = ship-to region; effective on order_date"),
        _date("order_date", "Date the order was placed.", rule="order_date <= as-of date"),
        Field("order_qty", "integer", "Ordered quantity.", unit="purchase unit", gt=0,
              rule=">= moq and a multiple of order_multiple"),
        _enum("order_uom", UOMS, "Purchase unit.", rule="= supplier_sku.purchase_uom"),
        Field("uom_conversion", "integer", "EA per purchase unit.", unit="EA/purchase unit", ge=1,
              rule="= supplier_sku.uom_conversion"),
        _qty("qty_base_ea", "Ordered quantity in EA.", rule="= order_qty x uom_conversion"),
        _week("expected_receipt_week", "Planned receipt week.",
              rule="= week(order_date) + lead_time.total_lead_weeks (may fall in 2025 for received POs)",
              any_year=True),
        Field("delay_weeks", "integer", "Transport delay.", unit="weeks", ge=0, le=6),
        _week("revised_receipt_week", "Expected receipt after delay.",
              rule="= expected_receipt_week + delay_weeks", any_year=True),
        _enum("status", PO_STATUS, "Current status.", rule="= to_status of the last purchase_order_event"),
        _enum("created_by_role", ("PLANNER",), "Role that raised the order."),
    ),
)

PURCHASE_ORDER_EVENT = Table(
    "purchase_order_event", "Append-only PO status history.",
    KIND_INPUT, "orders", lifecycle="purchase_order",
    pk=("event_id",), natural_key=("po_id", "seq"),
    fks=(fk("po_id", "purchase_order", label="has history"),),
    fields=(
        _id("event_id", "po_event", "POE-nnnnnnnn.", pk=True),
        _id("po_id", "purchase_order", "Purchase order."),
        Field("seq", "integer", "1-based event sequence within the PO.", ge=1,
              rule="consecutive per po_id"),
        _enum("from_status", PO_STATUS, "Status before the event; empty for the creation event.",
              required=False, rule="= previous event's to_status"),
        _enum("to_status", PO_STATUS, "Status after the event.", rule="allowed transition from from_status"),
        Field("event_ts", "datetime", "Event timestamp.",
              rule="strictly increasing per po_id; >= order_date; <= as-of date"),
        _enum("actor_role", PO_ACTOR_ROLES, "Who performed the transition."),
        _enum("reason_code", PO_REASON_CODES, "Why the transition happened."),
    ),
)

INVENTORY_TXN = Table(
    "inventory_txn", "Append-only inventory ledger from 2025-09-01 up to the as-of date.",
    KIND_INPUT, "inventory",
    pk=("txn_id",),
    fks=(fk("sku_id", "sku", label="moves"), fk("location_id", "location", label="records"),
         fk(("sku_id", "location_id"), "sku_location", label="ledger of"),
         fk("ref_id", "purchase_order", "po_id", optional=True, label="received as")),
    notes=("One OPENING row per SKU-location, RECEIPT rows from received POs, one aggregated ISSUE row "
           "per SKU-location, occasional cycle-count ADJUSTMENTs.",),
    fields=(
        _id("txn_id", "txn", "TXN-nnnnnnnn.", pk=True),
        _id("sku_id", "sku", "SKU."),
        _id("location_id", "location", "Location."),
        _enum("txn_type", TXN_TYPES, "Movement type."),
        _qty("qty_ea", "Signed quantity (+ in, - out).", signed=True,
             rule="OPENING/RECEIPT > 0; ISSUE < 0; ADJUSTMENT != 0"),
        _date("txn_date", "Posting date.", rule="2025-09-01 <= txn_date <= as-of date"),
        _id("ref_id", "purchase_order", "PO received (RECEIPT rows only).", required=False,
            rule="required iff txn_type = RECEIPT"),
    ),
)

INVENTORY = Table(
    "inventory", "Inventory snapshot at the as-of date: ON_HAND and IN_TRANSIT.",
    KIND_INPUT, "inventory", core_entity="Inventory",
    pk=("inventory_id",),
    fks=(fk("sku_id", "sku", label="held as"), fk("location_id", "location", label="holds"),
         fk(("sku_id", "location_id"), "sku_location", label="snapshot of"),
         fk("po_id", "purchase_order", optional=True, card="1:0..1", label="in transit as")),
    notes=("ON_HAND = sum of inventory_txn.qty_ea per SKU-location.",
           "IN_TRANSIT rows mirror open POs in SHIPPED / IN_TRANSIT / DELAYED; informational only — "
           "scheduled receipts come from purchase_order.",),
    fields=(
        _id("inventory_id", "inventory", "INV-nnnnnnn.", pk=True),
        _id("sku_id", "sku", "SKU."),
        _id("location_id", "location", "Location."),
        _enum("inventory_status", INVENTORY_STATUS, "Stock state."),
        _qty("qty_ea", "Quantity.", rule="ON_HAND = ledger sum; IN_TRANSIT = purchase_order.qty_base_ea"),
        _date("as_of_date", "Snapshot date.", rule="= config as_of_date"),
        _id("po_id", "purchase_order", "Inbound PO (IN_TRANSIT only).", required=False,
            rule="required iff IN_TRANSIT; at most one IN_TRANSIT row per po_id"),
    ),
)

PRODUCTION_PLAN = Table(
    "production_plan", "Production of MAKE SKUs at plants. FIRM weeks are input; PLANNED weeks come from the planner.",
    KIND_MIXED, "production", core_entity="ProductionPlan",
    pk=("prod_plan_id",), natural_key=("sku_id", "plant_location_id", "week"),
    fks=(fk("sku_id", "sku", label="produced as"),
         fk("plant_location_id", "location", "location_id", label="produces"),
         fk(("sku_id", "plant_location_id"), "sku_location", ("sku_id", "location_id"), label="supplies")),
    fields=(
        _id("prod_plan_id", "production_plan", "PROD-2026-nnnnnn.", pk=True),
        _id("sku_id", "sku", "MAKE SKU.", rule="sku.sourcing_type = MAKE"),
        _id("plant_location_id", "location", "Plant.", rule="location.location_type = PLANT"),
        _week("week", "Production (and receipt) week."),
        _qty("planned_qty_ea", "Quantity produced.", rule="<= requested_qty_ea"),
        _enum("status", PRODUCTION_STATUS, "FIRM in the first 4 weeks, PLANNED afterwards."),
        _qty("capacity_used_ea", "Plant capacity consumed.",
             rule="= planned_qty_ea; sum per plant-week <= production_capacity"),
        _qty("requested_qty_ea", "Net requirement before capacity / component cuts."),
        _qty("capacity_cut_ea", "Quantity cut because plant capacity was full."),
        _qty("component_cut_ea", "Quantity cut because a component was short.",
             rule="requested_qty_ea - capacity_cut_ea - component_cut_ea = planned_qty_ea"),
    ),
)

EXCEPTION = Table(
    "exception", "Quality-gate and policy exceptions with the action taken (the exception path).",
    KIND_OUTPUT, "planner",
    pk=("exception_id",),
    polymorphic_refs=("record_id",),
    notes=("record_id points at the offending record in the table named by `entity`.",),
    fields=(
        _id("exception_id", "exception", "EXC-2026-nnnnnn.", pk=True),
        _enum("issue_type", ISSUE_TYPES, "Detected issue."),
        _enum("entity", ("inventory", "purchase_order", "mitigation_option"), "Table of the offending record."),
        _text("record_id", "ID of the offending record.", rule="resolves to a row of `entity`"),
        _text("related_record_ids", "Other records involved, ';'-separated.", required=False,
              rule="each ID resolves to an existing record"),
        _text("detection_rule", "Rule that fired (see docs/business_rules.md)."),
        _enum("action_taken", EXCEPTION_ACTIONS, "How the planner handled it."),
        _enum("severity", SEVERITY, "Impact severity."),
        _text("detail", "Human-readable detail with quantities.", required=False),
    ),
)

PLANNED_ORDER = Table(
    "planned_order", "MRP planned purchase orders created by the planner.",
    KIND_OUTPUT, "planner",
    pk=("planned_order_id",),
    fks=(fk("supplier_id", "supplier", label="planned with"), fk("sku_id", "sku", label="planned for"),
         fk("location_id", "location", label="planned to"),
         fk("supplier_sku_id", "supplier_sku", label="planned under"),
         fk("lead_time_id", "lead_time", label="planned on"),
         fk("capacity_id", "supplier_capacity", label="consumes"),
         fk(("sku_id", "location_id"), "sku_location", label="replenishes")),
    fields=(
        _id("planned_order_id", "planned_order", "PLN-2026-nnnnnn.", pk=True),
        _id("supplier_id", "supplier", "Primary supplier."),
        _id("sku_id", "sku", "BUY SKU."),
        _id("location_id", "location", "Receiving location."),
        _id("supplier_sku_id", "supplier_sku", "Primary sourcing line."),
        _id("lead_time_id", "lead_time", "Standard lane effective at release."),
        _id("capacity_id", "capacity", "Supplier capacity row of the receipt week."),
        _week("release_week", "Order release week.", rule="= receipt_week - total_lead_weeks; >= 2026W01"),
        _week("receipt_week", "Planned receipt week."),
        Field("order_qty", "integer", "Quantity in purchase UoM.", unit="purchase unit", ge=0,
              rule=">= moq and a multiple of order_multiple when > 0"),
        _enum("order_uom", UOMS, "Purchase unit."),
        _qty("qty_base_ea", "Quantity in EA after capacity cut.", rule="= order_qty x uom_conversion"),
        _qty("requested_qty_ea", "Rounded requirement before capacity allocation."),
        _qty("capacity_cut_ea", "Quantity that did not fit supplier capacity.",
             rule="= requested_qty_ea - qty_base_ea"),
    ),
)

PROJECTED_BALANCE = Table(
    "projected_balance", "Projected available balance (PAB) per SKU-location-week.",
    KIND_OUTPUT, "planner",
    pk=("sku_id", "location_id", "week"),
    fks=(fk("sku_id", "sku", label="projected for"), fk("location_id", "location", label="projected at"),
         fk(("sku_id", "location_id"), "sku_location", label="projection of")),
    notes=("Backlog semantics: unmet demand carries forward as negative balance.",),
    fields=(
        _id("sku_id", "sku", "SKU."),
        _id("location_id", "location", "Location."),
        _week("week", "Planning week."),
        _qty("opening_qty", "Opening balance.", signed=True,
             rule="W01 = ON_HAND; otherwise = previous week's closing_qty"),
        _qty("firm_receipts", "Open PO receipts at revised receipt week (after quality-gate corrections)."),
        _qty("planned_receipts", "Planned-order receipts."),
        _qty("production_receipts", "Production output (MAKE SKUs at plants)."),
        _qty("demand_qty", "Forecast demand, or dependent demand for components."),
        _qty("closing_qty", "PAB.", signed=True,
             rule="= opening + firm + planned + production - demand"),
        _qty("safety_stock_qty", "Safety stock target (from sku_location)."),
        _enum("balance_status", BALANCE_STATUS, "Status of closing_qty.",
              rule="STOCKOUT if closing < 0; BELOW_SS if closing < safety stock; else OK"),
    ),
)

SHORTAGE = Table(
    "shortage", "Contiguous runs of weeks with PAB below safety stock for one SKU-location.",
    KIND_OUTPUT, "planner", core_entity="Shortage", lifecycle="shortage",
    pk=("shortage_id",), natural_key=("sku_id", "location_id", "start_week"),
    fks=(fk("sku_id", "sku", label="short on"), fk("location_id", "location", label="short at"),
         fk(("sku_id", "location_id"), "sku_location", label="shortage of")),
    notes=("Numbered after sorting by (start_week, sku_id, location_id).",),
    fields=(
        _id("shortage_id", "shortage", "SHORT-2026-nnnnnn.", pk=True),
        _id("sku_id", "sku", "SKU."),
        _id("location_id", "location", "Location."),
        _week("start_week", "First week below safety stock."),
        _week("end_week", "Last consecutive week below safety stock.", rule=">= start_week"),
        _week("peak_week", "Week of minimum closing_qty.", rule="start_week <= peak_week <= end_week"),
        _enum("shortage_type", SHORTAGE_TYPES, "STOCKOUT if closing < 0 in any week of the run."),
        _qty("shortage_qty_ea", "Unfilled quantity.", rule="= max(0, -min closing_qty in the run)"),
        _qty("below_ss_qty_ea", "Gap to safety stock.", rule="= max(safety_stock - closing_qty) in the run"),
        _money("value_at_risk_inr", "Revenue at risk.", rule="= shortage_qty_ea x sku.unit_price_inr"),
        _enum("primary_cause", CAUSE_CODES, "Cause with the largest contribution.",
              rule="one of this shortage's shortage_cause rows"),
        _text("explanation", "One generated sentence naming quantities, weeks and evidence IDs."),
        Field("priority_score", "decimal", "Priority score.", decimals=4, ge=0,
              rule="= value_at_risk x criticality weight (3/2/1) x 1/sqrt(1 + weeks from as-of to start)"),
        Field("priority_rank", "integer", "1 = most urgent.", ge=1, unique=True,
              rule="dense order by priority_score desc, then shortage_id"),
        _enum("status", SHORTAGE_STATUS, "Current status.", rule="= to_status of the last shortage_event"),
    ),
)

SHORTAGE_CAUSE = Table(
    "shortage_cause", "Bridge (Shortage N:M evidence): each derived cause with its evidence record.",
    KIND_OUTPUT, "planner",
    pk=("shortage_id", "cause_code", "evidence_id"),
    fks=(fk("shortage_id", "shortage", label="explained by"),),
    polymorphic_refs=("evidence_id",),
    fields=(
        _id("shortage_id", "shortage", "Shortage."),
        _enum("cause_code", CAUSE_CODES, "Rule-based cause."),
        _enum("evidence_type", EVIDENCE_TYPES, "Table holding the evidence record."),
        _text("evidence_id", "Evidence record ID.", rule="resolves to a row of evidence_type"),
        _qty("contribution_qty_ea", "Quantity attributed to this cause (0 when not quantifiable)."),
    ),
)

MITIGATION_OPTION = Table(
    "mitigation_option", "Candidate mitigations per shortage with cost/service trade-offs.",
    KIND_OUTPUT, "planner", core_entity="MitigationOption",
    pk=("mitigation_id",), natural_key=("shortage_id", "option_seq"),
    fks=(fk("shortage_id", "shortage", label="mitigated by"),
         fk("ref_po_id", "purchase_order", "po_id", optional=True, label="expedited by"),
         fk("ref_supplier_id", "supplier", "supplier_id", optional=True, label="alternate for"),
         fk("ref_lead_time_id", "lead_time", "lead_time_id", optional=True, label="lane for"),
         fk("ref_from_location_id", "location", "location_id", optional=True, label="transfers from"),
         fk("ref_substitute_id", "sku_substitute", "substitute_id", optional=True, label="applied in"),
         fk("ref_substitute_sku_id", "sku", "sku_id", optional=True, label="substitutes in"),
         fk("ref_plant_location_id", "location", "location_id", optional=True, label="produces for")),
    notes=("MANUAL options (source = MANUAL, created_by_role = PLANNER) are injected after `plan` and "
           "checked by `decide`.",
           "score = w_coverage x coverage + w_timeliness x timeliness - w_cost x normalised cost (weights in config).",),
    fields=(
        _id("mitigation_id", "mitigation", "MIT-<shortage_id>-nn.", pk=True,
            rule="= 'MIT-' + shortage_id + '-' + 2-digit option_seq"),
        _id("shortage_id", "shortage", "Shortage being mitigated."),
        Field("option_seq", "integer", "Option number within the shortage.", ge=1, le=99),
        _enum("option_type", MITIGATION_TYPES, "Mitigation lever."),
        _enum("source", MITIGATION_SOURCES, "SYSTEM-generated or MANUAL planner proposal."),
        _enum("created_by_role", MITIGATION_CREATORS, "Who proposed it.",
              rule="SYSTEM <=> source = SYSTEM"),
        _id("ref_po_id", "purchase_order", "PO to expedite.", required=False,
            rule="required for EXPEDITE_PO"),
        _id("ref_supplier_id", "supplier", "Alternate supplier.", required=False,
            rule="required for ALTERNATE_SUPPLIER"),
        _id("ref_lead_time_id", "lead_time", "Lane used (AIR lane for expedite).", required=False),
        _id("ref_from_location_id", "location", "Source location of a transfer.", required=False,
            rule="required for TRANSFER; != shortage location"),
        _id("ref_substitute_id", "substitute", "Substitution rule used.", required=False,
            rule="required for SUBSTITUTE_SKU"),
        _id("ref_substitute_sku_id", "sku", "Substitute SKU.", required=False,
            rule="= sku_substitute.substitute_sku_id"),
        _id("ref_plant_location_id", "location", "Plant producing extra.", required=False,
            rule="required for INCREASE_PRODUCTION"),
        _qty("action_qty_ea", "Physical quantity ordered / expedited / transferred / substituted / produced "
             "(EA of the SKU moved); 0 for BACKORDER."),
        _qty("qty_covered_ea", "Reduction of the run's largest gap to safety stock.",
             rule="<= shortage.below_ss_qty_ea (the requirement)"),
        _rate("coverage_pct", "qty_covered_ea / shortage.below_ss_qty_ea.", unit="ratio 0-1"),
        _week("arrival_week", "When the covered quantity becomes available.", required=False),
        Field("weeks_late", "integer", "Weeks after shortage start_week the cover arrives.", unit="weeks", ge=0),
        _money("incremental_cost_inr", "Extra cost of the option."),
        _rate("fill_rate_after", "SKU-location fill rate over the run if this option is taken.",
              rule="1 - sum(min(demand, backlog)) / sum(demand) over the run weeks"),
        Field("score", "decimal", "Service gain minus normalised cost.", decimals=4),
        _enum("policy_status", POLICY_STATUS, "Policy / feasibility verdict."),
        _text("policy_violation_reason", "Why the option violates policy.", required=False,
              rule="required iff policy_status = VIOLATION"),
        _bool("requires_approval", "Needs OPERATIONS_MANAGER approval.",
              rule="cost > approval threshold or > 25% of value at risk (SS-gap value when value at risk is 0)"),
        _bool("is_recommended", "Recommended option.",
              rule="at most one per shortage; only COMPLIANT options"),
    ),
)

MITIGATION_LEDGER = Table(
    "mitigation_ledger", "Capacity ledger entries booked by recommended mitigations, in priority-rank order.",
    KIND_OUTPUT, "planner",
    pk=("mitigation_id", "entry_seq"),
    fks=(fk("mitigation_id", "mitigation_option", label="books"),
         fk("supplier_id", "supplier", optional=True, label="consumes capacity of"),
         fk("plant_location_id", "location", "location_id", optional=True, label="consumes capacity of"),
         fk(("sku_id", "location_id"), "sku_location", optional=True, label="moves stock of")),
    notes=("Only recommended options (incl. escalated ones, reserved pending approval) book entries.",
           "SUPPLIER_CAPACITY / PLANT_CAPACITY: qty consumed in that week (negative = released, e.g. the old week "
           "of an expedited PO). STOCK: change of the SKU-location's projected closing from that week on "
           "(a draw of q over weeks t..b-1 is -q at t and +q at b, when a backfill order lands).",
           "Checks: firm + planned receipts + SUPPLIER entries <= supplier capacity; production + PLANT entries <= "
           "plant capacity; wherever a SKU-location's cumulative STOCK entries are negative its closing stays >= SS.",),
    fields=(
        _id("mitigation_id", "mitigation", "Recommended option that booked the entry."),
        Field("entry_seq", "integer", "Entry number within the option.", ge=1),
        _enum("resource_type", LEDGER_RESOURCES, "Ledger resource."),
        _id("supplier_id", "supplier", "Supplier (SUPPLIER_CAPACITY).", required=False,
            rule="required iff resource_type = SUPPLIER_CAPACITY"),
        _id("plant_location_id", "location", "Plant (PLANT_CAPACITY).", required=False,
            rule="required iff resource_type = PLANT_CAPACITY"),
        _id("sku_id", "sku", "SKU (STOCK).", required=False, rule="required iff resource_type = STOCK"),
        _id("location_id", "location", "Location (STOCK).", required=False, rule="required iff resource_type = STOCK"),
        _week("week", "Week of the entry."),
        _qty("qty_ea", "Signed quantity (see notes).", signed=True),
    ),
)

SHORTAGE_EVENT = Table(
    "shortage_event", "Append-only shortage status history.",
    KIND_OUTPUT, "planner", lifecycle="shortage",
    pk=("event_id",), natural_key=("shortage_id", "seq"),
    fks=(fk("shortage_id", "shortage", label="has history"),
         fk("mitigation_id", "mitigation_option", optional=True, label="selected in")),
    fields=(
        _id("event_id", "shortage_event", "SHE-nnnnnnn.", pk=True),
        _id("shortage_id", "shortage", "Shortage."),
        Field("seq", "integer", "1-based event sequence within the shortage.", ge=1),
        _enum("from_status", SHORTAGE_STATUS, "Previous status; empty for OPEN.", required=False),
        _enum("to_status", SHORTAGE_STATUS, "New status.", rule="allowed transition"),
        Field("event_ts", "datetime", "Planning-run timestamp (as-of date + run sequence).",
              rule="strictly increasing per shortage_id"),
        _enum("actor_role", SHORTAGE_ACTOR_ROLES, "Who performed the transition."),
        _enum("reason_code", SHORTAGE_REASON_CODES, "Why."),
        _id("mitigation_id", "mitigation", "Option selected / escalated.", required=False),
    ),
)

MITIGATION_PROPOSAL = Table(
    "mitigation_proposal", "MANUAL mitigation proposals entered by planners for real shortages, checked by decide.",
    KIND_INPUT, "planner (manual)",
    pk=("mitigation_id",), natural_key=("shortage_id", "option_seq"),
    fks=(fk("shortage_id", "shortage", label="proposed for"),
         fk("ref_substitute_id", "sku_substitute", "substitute_id", optional=True, label="proposes"),
         fk("ref_substitute_sku_id", "sku", "sku_id", label="proposes substitute")),
    notes=("Entered after a first planning pass (generate -> plan -> proposals -> decide); decide turns each into a "
           "mitigation_option with source = MANUAL and the same mitigation_id.",),
    fields=(
        _id("mitigation_id", "mitigation", "MIT-<shortage_id>-nn; option_seq >= 90 so it never clashes with "
            "SYSTEM options.", pk=True, rule="= 'MIT-' + shortage_id + '-' + 2-digit option_seq"),
        _id("shortage_id", "shortage", "Shortage the planner wants to mitigate."),
        Field("option_seq", "integer", "Option number within the shortage.", ge=90, le=99),
        _enum("option_type", ("SUBSTITUTE_SKU",), "Proposed lever (planners propose substitutions)."),
        _id("ref_substitute_id", "substitute", "Substitution rule proposed; empty for an ad-hoc substitute with no "
            "rule.", required=False),
        _id("ref_substitute_sku_id", "sku", "Substitute SKU proposed.",
            rule="= sku_substitute.substitute_sku_id when ref_substitute_id is set"),
        _qty("qty_proposed_ea", "Quantity of the original SKU to cover.", gt=0),
        _enum("created_by_role", ("PLANNER",), "Who proposed it."),
    ),
)

GROUND_TRUTH = Table(
    "ground_truth", "Labels for injected anomalies and boundary cases. Never an input to the planner.",
    KIND_LABEL, "labels",
    pk=("gt_id",),
    polymorphic_refs=("record_id",),
    notes=("Written only by planner.inject; read only by planner.evaluate.",),
    fields=(
        _id("gt_id", "gt", "GT-nnnnn.", pk=True),
        _enum("case_class", CASE_CLASSES, "ANOMALY must be detected; BOUNDARY must not raise an exception."),
        _enum("issue_type", ISSUE_TYPES + BOUNDARY_TYPES, "Injected case type."),
        _text("entity", "Table of the targeted record."),
        _text("record_id", "Targeted record ID."),
        _text("related_record_ids", "Related records, ';'-separated.", required=False),
        _text("injection_rule", "What was changed."),
        _text("expected_detection_rule", "Rule expected to fire (or 'none' for boundary cases)."),
        _text("expected_action", "Expected exception action (or 'none')."),
        _text("expected_outcome", "Expected planning outcome, e.g. 'shortage SKU-…/LOC-… starting 2026Wnn'."),
    ),
)

TABLES: tuple[Table, ...] = (
    LOCATION, SKU, SUPPLIER,
    SKU_LOCATION, SUPPLIER_SKU, SKU_SUBSTITUTE, BOM,
    LEAD_TIME, SUPPLIER_CAPACITY, PRODUCTION_CAPACITY,
    FORECAST_DEMAND, PURCHASE_ORDER, PURCHASE_ORDER_EVENT, INVENTORY_TXN, INVENTORY, PRODUCTION_PLAN,
    EXCEPTION, PLANNED_ORDER, PROJECTED_BALANCE, SHORTAGE, SHORTAGE_CAUSE, MITIGATION_OPTION, MITIGATION_LEDGER, SHORTAGE_EVENT,
    MITIGATION_PROPOSAL, GROUND_TRUTH,
)

BY_NAME: dict[str, Table] = {t.name: t for t in TABLES}

CORE_ENTITIES = (
    "SKU", "Location", "ForecastDemand", "Inventory", "Supplier", "SupplierCapacity", "LeadTime",
    "PurchaseOrder", "ProductionPlan", "Shortage", "MitigationOption",
)

# Explicit many-to-many relationships resolved by bridge tables.
MANY_TO_MANY: tuple[tuple[str, str, str, str], ...] = (
    ("sku", "location", "sku_location", "Assortment: a SKU is stocked at many locations; a location stocks many SKUs."),
    ("supplier", "sku", "supplier_sku", "Sourcing: a SKU has 1 primary + 0-2 alternate suppliers; a supplier sells many SKUs."),
    ("sku", "sku", "sku_substitute", "Substitution: a SKU may have several substitutes and substitute for several SKUs."),
    ("sku", "sku", "bom", "BOM: a MAKE SKU uses 2-4 components; a component is used in many parents."),
    ("shortage", "evidence records", "shortage_cause", "Causes: a shortage has many causes; an evidence record can explain many shortages."),
)

# --------------------------------------------------------------------------------------------
# Lifecycles (state machines)
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Lifecycle:
    name: str
    entity: str
    states: tuple[str, ...]
    initial: tuple[str, ...]
    transitions: tuple[tuple[str, str], ...]
    terminal: tuple[str, ...]
    description: str
    event_table: str | None = None

    def allowed(self, src: str | None, dst: str) -> bool:
        if src is None:
            return dst in self.initial
        return (src, dst) in self.transitions


LIFECYCLES: dict[str, Lifecycle] = {
    "purchase_order": Lifecycle(
        "purchase_order", "purchase_order", PO_STATUS, ("CREATED",),
        (("CREATED", "CONFIRMED"), ("CONFIRMED", "SHIPPED"), ("SHIPPED", "IN_TRANSIT"),
         ("IN_TRANSIT", "RECEIVED"), ("IN_TRANSIT", "DELAYED"), ("DELAYED", "IN_TRANSIT"),
         ("CREATED", "CANCELLED"), ("CONFIRMED", "CANCELLED")),
        ("RECEIVED", "CANCELLED"),
        "Every PO starts CREATED. purchase_order.status = last event's to_status. No event after as-of.",
        event_table="purchase_order_event",
    ),
    "shortage": Lifecycle(
        "shortage", "shortage", SHORTAGE_STATUS, ("OPEN",),
        (("OPEN", "MITIGATION_PROPOSED"), ("MITIGATION_PROPOSED", "MITIGATION_SELECTED"),
         ("MITIGATION_PROPOSED", "ESCALATED"), ("MITIGATION_PROPOSED", "BACKORDER_ACCEPTED")),
        ("MITIGATION_SELECTED", "ESCALATED", "BACKORDER_ACCEPTED"),
        "ESCALATED when the best compliant option needs OPERATIONS_MANAGER approval; BACKORDER_ACCEPTED "
        "when no feasible non-backorder option exists.",
        event_table="shortage_event",
    ),
    "sku_lifecycle": Lifecycle(
        "sku_lifecycle", "sku", SKU_LIFECYCLE, ("NEW", "ACTIVE"),
        (("NEW", "ACTIVE"), ("ACTIVE", "PHASE_OUT")),
        ("PHASE_OUT",),
        "Snapshot state at the as-of date (master data, no event table). PHASE_OUT SKUs carry effective_to.",
    ),
    "supplier_status": Lifecycle(
        "supplier_status", "supplier", SUPPLIER_STATUS, ("ACTIVE",),
        (("ACTIVE", "ON_HOLD"), ("ON_HOLD", "ACTIVE"), ("ACTIVE", "INACTIVE"), ("ON_HOLD", "INACTIVE")),
        ("INACTIVE",),
        "Snapshot status at the as-of date. Only ACTIVE suppliers may be used for new orders.",
    ),
    "approval": Lifecycle(
        "approval", "supplier_sku / sku_substitute", APPROVAL, ("PENDING",),
        (("PENDING", "APPROVED"), ("PENDING", "REJECTED")),
        ("APPROVED", "REJECTED"),
        "Approval of sourcing lines and substitutes. Only APPROVED and effective records may be used.",
    ),
}


def get(name: str) -> Table:
    return BY_NAME[name]
