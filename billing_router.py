# billing_router.py

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Dict, Any, Tuple
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

# main.py から認証依存とテナント所属チェックをインポート
from main import fastapi_auth, belonging_tenant

# SaaS SDK のクライアント
from saasus_sdk_python.src.auth import TenantApi
from saasus_sdk_python.src.pricing import (
    PricingPlansApi,
    MeteringApi,
    UpdateMeteringUnitTimestampCountParam,
    UpdateMeteringUnitTimestampCountMethod,
)
from saasus_sdk_python.client.auth_client import SignedAuthApiClient
from saasus_sdk_python.client.pricing_client import SignedPricingApiClient

# API クライアント初期化
api_client = SignedAuthApiClient()
pricing_api_client = SignedPricingApiClient()

router = APIRouter(
    tags=["billing"],
)

# --- リクエストボディモデル ---
class UpdateCountBody(BaseModel):
    method: str = Field(..., pattern="^(add|sub|direct)$")
    count: int = Field(..., ge=0)


# --- 認可ヘルパー ---
def has_billing_access(auth_user: Any, tenant_id: str) -> bool:
    # テナント所属チェック
    if not belonging_tenant(auth_user.tenants, tenant_id):
        return False
    # 指定テナントのロール確認
    for tenant in auth_user.tenants:
        if tenant.id != tenant_id:
            continue
        for env in tenant.envs:
            for role in env.roles:
                if role.role_name in ("admin", "sadmin"):
                    return True
    return False


# --- プラン年単位判定ヘルパー ---
def plan_has_year_unit(plan: Any) -> bool:
    for menu in plan.pricing_menus:
        for unit in menu.units:
            inst = getattr(unit, "actual_instance", unit)
            recurring_interval = getattr(inst, "recurring_interval", None)
            if recurring_interval is None:
                continue
            if recurring_interval.value == "year":
                return True
    return False

# --- 内部ユーティリティ関数 ---
def extract_tiers(u: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "to": int(m.get("up_to", 0)),
            "inf": bool(m.get("inf", False)),
            "flat_amount": float(m.get("flat_amount", 0)),
            "unit_price": float(m.get("unit_amount", 0)),
        }
        for m in u.get("tiers", []) if isinstance(m, dict)
    ]

def calc_tiered(count: float, unit_dict: Dict[str, Any]) -> float:
    tiers = extract_tiers(unit_dict)
    last = None
    for tier in tiers:
        last = tier
        if tier["inf"] or count <= t["to"]:
            return tier["flat_amount"] + count * tier["unit_price"]
    if last:
        return last["flat_amount"] + count * last["unit_price"]
    return 0.0

def calc_tiered_usage(count: float, unit_dict: Dict[str, Any]) -> float:
    tiers = extract_tiers(unit_dict)
    total = 0.0
    prev = 0.0
    for tier in tiers:
        if count <= prev:
            break
        usage = (count - prev) if tier["inf"] else (min(count, tier["to"]) - prev)
        total += tier["flat_amount"] + usage * tier["unit_price"]
        prev = tier["to"]
    return total


def calculate_amount_by_unit_type(count: float, unit_dict: Dict[str, Any]) -> float:
    unit_type = unit_dict.get("type", "usage")
    price = float(unit_dict.get("unit_amount", 0))
    if unit_type == "fixed":
        return price
    if unit_type == "usage":
        return count * price
    if unit_type == "tiered":
        return calc_tiered(count, unit_dict)
    if unit_type == "tiered_usage":
        return calc_tiered_usage(count, unit_dict)
    return count * price

def calculate_metering_unit_billings(
    tenant_id: str,
    period_start: int,
    period_end: int,
    plan: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    billings: List[Dict[str, Any]] = []
    currency_sum: Dict[str, float] = {}
    usage_cache: Dict[str, float] = {}

    for menu in plan.pricing_menus:
        menu_name = menu.display_name
        for unit in menu.units:
            unit_dict = unit.to_dict()
            unit_name = unit_dict.get("metering_unit_name", "")
            unit_type = unit_dict.get("type", "usage")
            agg_usage = unit_dict.get("aggregate_usage", "sum")

            count = usage_cache.get(unit_name, 0.0)
            if unit_type != "fixed" and count == 0:
              resp = MeteringApi(api_client=pricing_api_client).get_metering_unit_date_count_by_tenant_id_and_unit_name_and_date_period(
                tenant_id=tenant_id,
                metering_unit_name=unit_name,
                start_timestamp=period_start,
                end_timestamp=period_end,
              )
              counts = resp.counts

              if agg_usage == "max":
                  # 最大値を返す
                  count = max((c.count for c in counts), default=0)
              else:
                  # 合計値を返す
                  count = sum(c.count for c in counts)
                  usage_cache[unit_name] = count

            amount = calculate_amount_by_unit_type(count, unit_dict)
            curr = unit_dict.get("currency", "")
            disp_name = unit_dict.get("display_name", "")

            billings.append({
                "metering_unit_name": unit_name,
                "function_menu_name": menu_name,
                "period_count": count,
                "currency": curr,
                "period_amount": amount,
                "pricing_unit_display_name": disp_name,
            })
            currency_sum[curr] = currency_sum.get(curr, 0.0) + amount

    totals = [
        {"currency": c, "total_amount": currency_sum[c]}
        for c in sorted(currency_sum.keys())
    ]
    return billings, totals


# --- エンドポイント ---

@router.get(
    "/billing/dashboard",
    summary="Get billing dashboard",
)
def get_billing_dashboard(
    tenant_id: str = Query(..., description="Tenant ID"),
    plan_id: str = Query(..., description="Pricing Plan ID"),
    period_start: int = Query(..., description="Period start timestamp (Unix seconds)"),
    period_end: int = Query(..., description="Period end timestamp (Unix seconds)"),
    auth_user: Any = Depends(fastapi_auth),
):
    if not has_billing_access(auth_user, tenant_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    plan = PricingPlansApi(api_client=pricing_api_client).get_pricing_plan(plan_id=plan_id)
    TenantApi(api_client=api_client).get_tenant(tenant_id=tenant_id)  # テナント検証のみ

    billings, totals = calculate_metering_unit_billings(
        tenant_id, period_start, period_end, plan
    )

    return {
        "summary": {"total_by_currency": totals, "total_metering_units": len(billings)},
        "metering_unit_billings": billings,
        "pricing_plan_info": {
            "plan_id": plan_id,
            "display_name": plan.display_name,
            "description": plan.description,
        },
    }


@router.get(
    "/tenant/plan_periods",
    summary="Get available plan periods for a tenant",
)
def get_plan_periods(
    tenant_id: str = Query(..., description="Tenant ID"),
    auth_user: Any = Depends(fastapi_auth),
) -> List[Dict[str, Any]]:
    # 権限チェック
    if not has_billing_access(auth_user, tenant_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # テナント取得
    tenant = TenantApi(api_client=SignedAuthApiClient()).get_tenant(tenant_id=tenant_id)

    # 1) 境界エッジ作成（PlanAppliedAt 昇順）
    edges = sorted(
        [
            {
                "plan_id": plan_history.plan_id or "",
                "applied_at": datetime.fromtimestamp(plan_history.plan_applied_at)
            }
            for plan_history in getattr(tenant, "plan_histories", [])
        ],
        key=lambda e: e["applied_at"]
    )

    # 2) 最終境界（current_plan_period_end があればその 1 秒前、無ければ「今」）
    if getattr(tenant, "current_plan_period_end", None):
        last_boundary = datetime.fromtimestamp(tenant.current_plan_period_end - 1)
    else:
        last_boundary = datetime.now()

    results: List[Dict[str, Any]] = []

    # 3) 各エッジごとに区間を分割
    pricing_client = SignedPricingApiClient()
    for idx, e in enumerate(edges):
        plan_id = e["plan_id"]
        if not plan_id:
            continue
        start_dt = e["applied_at"]
        end_dt = (
            edges[idx + 1]["applied_at"] - timedelta(seconds=1)
            if idx + 1 < len(edges)
            else last_boundary
        )

        # 4) この境界のプランを取得し、年単位／月単位を判定
        plan = PricingPlansApi(api_client=pricing_client).get_pricing_plan(plan_id=e["plan_id"])
        recurring = "year" if plan_has_year_unit(plan) else "month"

        cur = start_dt
        while cur <= end_dt:
            # 5) セグメントの終端を計算
            if recurring == "year":
                nxt = cur.replace(year=cur.year + 1)
            else:
                nxt = cur + relativedelta(months=1)

            seg_end = min(nxt - timedelta(seconds=1), end_dt)

            label = f"{cur:%Y年%m月%d日 %H:%M:%S} ～ {seg_end:%Y年%m月%d日 %H:%M:%S}"
            results.append({
                "label": label,
                "plan_id": e["plan_id"],
                "start": int(cur.timestamp()),
                "end": int(seg_end.timestamp()),
            })

            if seg_end >= end_dt:
                break
            cur = seg_end + timedelta(seconds=1)

    # 6) 新しい順にソートして返却
    results.sort(key=lambda x: x["start"], reverse=True)
    return results

@router.post(
    "/metering/{tenant_id}/{unit}/{ts}",
    summary="Update metering count at specified timestamp"
)
def update_metering_count(
    tenant_id: str,
    unit: str,
    ts: int,
    body: UpdateCountBody,
    auth_user: Any = Depends(fastapi_auth),
):
    if not has_billing_access(auth_user, tenant_id):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    param = UpdateMeteringUnitTimestampCountParam(
        method=UpdateMeteringUnitTimestampCountMethod(body.method),
        count=body.count,
    )
    resp = MeteringApi(api_client=pricing_api_client).update_metering_unit_timestamp_count(
        tenant_id=tenant_id,
        metering_unit_name=unit,
        timestamp=ts,
        update_metering_unit_timestamp_count_param=param,
    )
    return resp
