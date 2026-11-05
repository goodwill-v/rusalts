"""Ночной чек согласованности платформы (Этап E2 ТЗ Развития).

Проверяет «одним голосом»:
  1. Ключевые страницы и страницы продуктов отвечают 200.
  2. Цены витринных карточек = тарифам в shop.db (Этап A).
  3. Тарифные планы в API = строкам в tariff_plans.
  4. Кнопки покупки только у доступных продуктов («Скоро» не продаётся).

Отчёт: последняя сводка в процессе (админка может запросить и показать),
расхождения ночью — алерт в MAX (Шефу). HTTP-часть вынесена в fetch-колбэк,
чтобы регресс гонялся в CI без сети.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from app import config

Fetch = Callable[[str], Awaitable[tuple[int, str]]]

CORE_PAGES = (
    "/", "/techologis/", "/news/", "/consultant", "/diagnostics/",
    "/legal/privacy/", "/legal/consent/",
)


async def _http_fetch(url: str) -> tuple[int, str]:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url)
            return r.status_code, r.text
    except Exception as e:
        return 0, str(e)


async def run_consistency_check(
    *, fetch: Fetch | None = None, base_url: str = ""
) -> dict[str, Any]:
    """Полный чек. fetch — подменяемый загрузчик (в тестах: TestClient)."""
    from app.products_store import catalog
    from app.tariffs_store import list_plans, list_tariffs

    fetch = fetch or _http_fetch
    base = (base_url or config.PUBLIC_BASE_URL).rstrip("/")
    issues: list[str] = []
    checked = 0

    # 1. Ключевые страницы — 200
    for path in CORE_PAGES:
        checked += 1
        status, _ = await fetch(base + path)
        if status != 200:
            issues.append(f"страница {path} → {status or 'сбой сети'}")

    # 2. Цены витринных карточек = тарифам БД
    tariff_rows = {t["product_id"]: t for t in list_tariffs()}
    products = catalog()
    for p in products:
        t = tariff_rows.get(p["id"])
        if t and p.get("price_rub"):
            checked += 1
            if int(p["price_rub"]) != int(t["price_rub"]):
                issues.append(
                    f"{p['id']}: витрина {p['price_rub']}₽ ≠ тариф БД {t['price_rub']}₽"
                )

    # 3. Планы: API-сервер читает ту же БД — проверяем живые страницы продуктов
    buyable = [p for p in products
               if p.get("status") == "available" and p.get("price_rub", 0) > 0]
    buyable_ids = {p["id"] for p in buyable}
    for p in products:
        plans = list_plans(p["id"])
        if not plans:
            continue
        checked += 1
        status, html = await fetch(base + f"/techologis/p/{p['id']}/")
        if status != 200:
            issues.append(f"страница продукта {p['id']} → {status or 'сбой сети'}")
            continue
        for pl in plans:
            if f"{pl['price_rub']} ₽" not in html:
                issues.append(
                    f"{p['id']}: план {pl['plan']} ({pl['price_rub']}₽) не на странице"
                )

    # 4. «Скоро»/черновики не продаются на витрине
    checked += 1
    status, html = await fetch(base + "/techologis/")
    if status == 200:
        for pid in re.findall(r'data-product="([^"]+)"', html):
            if pid not in buyable_ids:
                issues.append(f"кнопка покупки у недоступного продукта: {pid}")
    else:
        issues.append(f"витрина /techologis/ → {status or 'сбой сети'}")

    return {
        "ok": not issues,
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "base_url": base,
        "checked": checked,
        "issues": issues,
    }


def format_alert(report: dict[str, Any]) -> str:
    """Компактный текст алерта для MAX (верхние 5 расхождений)."""
    head = "🔴 Чек согласованности: расхождения!" if report["issues"] \
        else "🟢 Чек согласованности: всё сходится"
    lines = [head, f"Проверок: {report['checked']} · {report['checked_at'][:16]}"]
    for it in report["issues"][:5]:
        lines.append(f"• {it}")
    if len(report["issues"]) > 5:
        lines.append(f"… ещё {len(report['issues']) - 5} (в админке)")
    return "\n".join(lines)
