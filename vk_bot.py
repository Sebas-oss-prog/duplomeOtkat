# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import socket
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import uvicorn
import vk_api
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from requests.exceptions import RequestException
from supabase import Client, create_client
from vk_api.exceptions import ApiError
from vk_api.longpoll import VkEventType, VkLongPoll
from vk_api.utils import get_random_id


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("raipo-b2b")

VK_TOKEN = os.getenv("VK_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
PORT = int(os.getenv("PORT", 7860))
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
BACKEND_PUBLIC_URL = os.getenv("BACKEND_PUBLIC_URL", "").strip().rstrip("/")
VK_APP_ID = os.getenv("VK_APP_ID", "").strip() or None
VK_APP_OWNER_ID = os.getenv("VK_APP_OWNER_ID", "").strip() or None
VK_APP_HASH = os.getenv("VK_APP_HASH", "").strip() or None
VK_GROUP_ID = abs(int(os.getenv("VK_APP_OWNER_ID", "0") or "0"))
ADMIN_ROLES = {"administrator", "creator", "moderator", "editor"}

if not all([VK_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    raise RuntimeError("Не найдены VK_TOKEN, SUPABASE_URL или SUPABASE_KEY в .env")

COMPANY_NAME = "Слободское РАЙПО"
COMPANY_REQUISITES = [
    "Поставщик: Слободское РАЙПО",
    "ИНН: 4329000000",
    "КПП: 432901001",
    "Р/с: 40702810000000000001",
    "Банк: ПАО СБЕРБАНК",
    "БИК: 043304609",
    "Email: b2b@raipo.local",
    "Телефон: +7 (83362) 4-00-00",
]

PAYMENT_TYPES = {"prepayment", "deferred"}
CONTRACTOR_STATUSES = {"active", "blocked", "new_request"}
DOCUMENT_REQUEST_TYPES = {"reconciliation_act", "duplicate_invoice"}
DOCUMENT_REQUEST_STATUSES = {"new", "in_progress", "done", "rejected"}
ORDER_STATUSES = {
    "new",
    "waiting_payment",
    "payment_check",
    "confirmed",
    "paid",
    "in_production",
    "shipped",
    "completed",
    "cancelled",
    "rejected",
}
PAID_ORDER_STATUSES = {"paid", "completed"}

ORDER_STATUS_LABELS = {
    "new": "Новая заявка",
    "waiting_payment": "Ожидает предоплату",
    "payment_check": "Проверка оплаты",
    "confirmed": "Подтверждена",
    "paid": "Оплачена",
    "in_production": "В обработке",
    "shipped": "Отгружена",
    "completed": "Завершена",
    "cancelled": "Отменена",
    "rejected": "Отклонена",
}
PAYMENT_TYPE_LABELS = {
    "prepayment": "Предоплата",
    "deferred": "Отсрочка платежа",
}
CONTRACTOR_STATUS_LABELS = {
    "active": "Активен",
    "blocked": "Заблокирован",
    "new_request": "Новая заявка",
}
DOCUMENT_REQUEST_TYPE_LABELS = {
    "reconciliation_act": "Акт сверки",
    "duplicate_invoice": "Дубликат накладной",
}
DOCUMENT_REQUEST_STATUS_LABELS = {
    "new": "Новый",
    "in_progress": "В работе",
    "done": "Выполнен",
    "rejected": "Отклонён",
}

BASE_DIR = Path(__file__).parent
INVOICE_DIR = BASE_DIR / "generated" / "invoices"
INVOICE_DIR.mkdir(parents=True, exist_ok=True)
DOCUMENT_DIR = BASE_DIR / "generated" / "documents"
DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
PDF_FONT_NAME = "RAIPOFont"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def normalize_identifier(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip())


def sanitize_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def money(value: Any) -> float:
    return round(float(value or 0), 2)


def current_year() -> int:
    return utcnow().year


def format_order_number(order_id: int, year: Optional[int] = None) -> str:
    return f"ORD-{year or current_year()}-{int(order_id):04d}"


def format_customer_order_number(value: Any) -> str:
    try:
        return f"№{int(value)}"
    except (TypeError, ValueError):
        return "—"


def build_document_file_url(file_name: str) -> str:
    file_url = f"/documents/files/{Path(file_name).name}"
    return f"{BACKEND_PUBLIC_URL}{file_url}" if BACKEND_PUBLIC_URL else file_url




def find_pdf_font_path() -> Optional[Path]:
    candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/tahoma.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    search_roots = [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".fonts",
        Path.home() / ".local" / "share" / "fonts",
    ]
    search_roots.extend(Path(item) for item in sys.path if item)
    font_names = {
        "DejaVuSans.ttf",
        "LiberationSans-Regular.ttf",
        "FreeSans.ttf",
        "Arial.ttf",
        "arial.ttf",
        "tahoma.ttf",
        "NotoSans-Regular.ttf",
    }
    for root in search_roots:
        try:
            if not root.exists():
                continue
            for pattern in font_names:
                direct = root / pattern
                if direct.exists():
                    return direct
                for found in root.rglob(pattern):
                    if found.exists():
                        return found
        except Exception:
            continue
    return None


def ensure_pdf_font_registered() -> str:
    if PDF_FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return PDF_FONT_NAME
    font_path = find_pdf_font_path()
    if not font_path:
        raise RuntimeError("Не найден шрифт с поддержкой кириллицы для генерации PDF")
    pdfmetrics.registerFont(TTFont(PDF_FONT_NAME, str(font_path)))
    return PDF_FONT_NAME


def build_invoice_pdf(
    order: Dict[str, Any],
    contractor: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> bytes:
    from io import BytesIO

    font_name = ensure_pdf_font_registered()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"Счет {order.get('id')}",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "InvoiceTitle",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=15,
        leading=19,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#1F2A1C"),
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "InvoiceBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10,
        leading=13,
        alignment=TA_LEFT,
        wordWrap="CJK",
        spaceAfter=3,
    )
    small_style = ParagraphStyle(
        "InvoiceSmall",
        parent=body_style,
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#4F5A4A"),
    )
    right_style = ParagraphStyle(
        "InvoiceRight",
        parent=body_style,
        alignment=TA_RIGHT,
    )

    story: List[Any] = [
        Paragraph(COMPANY_NAME, title_style),
        Paragraph(f"Счёт по заявке: {order.get('order_number') or format_order_number(int(order.get('id') or 0))}", body_style),
        Paragraph(f"Номер заявки контрагента: {format_customer_order_number(order.get('customer_order_number'))}", body_style),
        Paragraph(f"Дата: {utcnow().date().isoformat()}", body_style),
        Spacer(1, 4 * mm),
    ]

    for line in COMPANY_REQUISITES:
        story.append(Paragraph(line, small_style))

    story.extend(
        [
            Spacer(1, 6 * mm),
            Paragraph("Покупатель", body_style),
            Paragraph(f"Организация: {contractor.get('company_name') or '—'}", body_style),
            Paragraph(f"ИНН: {contractor.get('inn') or '—'}", body_style),
            Paragraph(f"Номер договора: {contractor.get('contract_number') or '—'}", body_style),
            Spacer(1, 6 * mm),
        ]
    )

    table_data: List[List[Any]] = [
        [
            Paragraph("№", body_style),
            Paragraph("Товар", body_style),
            Paragraph("Кол-во", body_style),
            Paragraph("Цена", body_style),
            Paragraph("Сумма", body_style),
        ]
    ]
    for index, item in enumerate(items, start=1):
        table_data.append(
            [
                Paragraph(str(index), body_style),
                Paragraph(str(item.get("name") or f"Товар #{item.get('product_id')}"), body_style),
                Paragraph(str(int(item.get("quantity") or 0)), right_style),
                Paragraph(f"{money(item.get('price')):.2f} ₽", right_style),
                Paragraph(f"{money(item.get('line_total')):.2f} ₽", right_style),
            ]
        )

    table = Table(
        table_data,
        repeatRows=1,
        colWidths=[12 * mm, 86 * mm, 22 * mm, 30 * mm, 30 * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("LEADING", (0, 0), (-1, -1), 12),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7E1D6")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1F2A1C")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B6AE9F")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(f"Итого к оплате: {money(order.get('total_amount')):.2f} ₽", title_style))
    story.append(Paragraph("Счет сформирован автоматически. Для запуска в производство требуется предоплата.", body_style))

    doc.build(story)
    return buffer.getvalue()


def build_reconciliation_pdf(contractor: Dict[str, Any], orders: List[Dict[str, Any]]) -> bytes:
    from io import BytesIO

    font_name = ensure_pdf_font_registered()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"Акт сверки {contractor.get('company_name') or ''}",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ReconTitle", parent=styles["Heading1"], fontName=font_name, fontSize=15, leading=19)
    body_style = ParagraphStyle("ReconBody", parent=styles["BodyText"], fontName=font_name, fontSize=10, leading=13)
    small_style = ParagraphStyle("ReconSmall", parent=body_style, fontSize=9, leading=12, textColor=colors.HexColor("#4F5A4A"))
    right_style = ParagraphStyle("ReconRight", parent=body_style, alignment=TA_RIGHT)

    total_amount = money(sum(money(order.get("total_amount")) for order in orders))
    current_debt = money(contractor.get("current_debt"))
    credit_limit = money(contractor.get("credit_limit"))
    available_limit = max(0.0, money(credit_limit - current_debt))

    story: List[Any] = [
        Paragraph("Акт сверки", title_style),
        Paragraph(f"Организация: {contractor.get('company_name') or '—'}", body_style),
        Paragraph(f"ИНН: {contractor.get('inn') or '—'}", body_style),
        Paragraph(f"Договор: {contractor.get('contract_number') or '—'}", body_style),
        Paragraph(f"Дата формирования: {utcnow().date().isoformat()}", body_style),
        Spacer(1, 4 * mm),
        Paragraph(f"Всего заказов: {len(orders)}", body_style),
        Paragraph(f"Сумма заказов: {total_amount:.2f} ₽", body_style),
        Paragraph(f"Текущая задолженность: {current_debt:.2f} ₽", body_style),
        Paragraph(f"Кредитный лимит: {credit_limit:.2f} ₽", body_style),
        Paragraph(f"Доступный остаток: {available_limit:.2f} ₽", body_style),
        Spacer(1, 6 * mm),
    ]

    table_data: List[List[Any]] = [[
        Paragraph("Заявка", body_style),
        Paragraph("Номер клиента", body_style),
        Paragraph("Дата", body_style),
        Paragraph("Сумма", body_style),
        Paragraph("Статус", body_style),
    ]]
    for order in orders[:20]:
        table_data.append([
            Paragraph(str(order.get("order_number") or format_order_number(int(order.get("id") or 0))), body_style),
            Paragraph(format_customer_order_number(order.get("customer_order_number")), body_style),
            Paragraph(str(order.get("created_at") or "")[:10] or "—", body_style),
            Paragraph(f"{money(order.get('total_amount')):.2f} ₽", right_style),
            Paragraph(str(ORDER_STATUS_LABELS.get(order.get("status"), order.get("status") or "—")), small_style),
        ])

    table = Table(table_data, repeatRows=1, colWidths=[46 * mm, 34 * mm, 28 * mm, 28 * mm, 44 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LEADING", (0, 0), (-1, -1), 12),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7E1D6")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B6AE9F")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    doc.build(story)
    return buffer.getvalue()

class Repo:
    def __init__(self, supabase_url: str, supabase_key: str) -> None:
        self._supabase: Client = create_client(supabase_url, supabase_key)

    def _supabase_request(self, func: Callable, *args, max_retries: int = 3, **kwargs):
        last_error = None
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # pragma: no cover - defensive wrapper
                last_error = exc
                error_text = str(exc).lower()
                if any(token in error_text for token in ["timeout", "connection", "read operation", "httpcore"]):
                    if attempt < max_retries - 1:
                        wait_seconds = attempt + 1
                        logger.warning("Supabase retry %s/%s in %ss", attempt + 1, max_retries, wait_seconds)
                        time.sleep(wait_seconds)
                        continue
                raise
        raise last_error

    def list_categories(self) -> List[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("categories").select("id,name").order("id").execute()
        )
        return result.data or []

    def list_products(self, category_id: Optional[int] = None, search: Optional[str] = None) -> List[Dict[str, Any]]:
        query = (
            self._supabase.table("products")
            .select("id,name,description,price,min_quantity,stock_quantity,category_id,image")
            .order("id")
        )
        if category_id:
            query = query.eq("category_id", category_id)
        if search:
            query = query.ilike("name", f"%{search.strip()}%")
        result = self._supabase_request(lambda: query.execute())
        return result.data or []

    def get_product(self, product_id: int) -> Optional[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("products")
            .select("id,name,description,price,min_quantity,stock_quantity,category_id,image")
            .eq("id", product_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    def update_product_stock_if_matches(self, product_id: int, expected_stock: int, new_stock: int) -> Optional[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("products")
            .update({"stock_quantity": new_stock})
            .eq("id", product_id)
            .eq("stock_quantity", expected_stock)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    def increment_product_stock(self, product_id: int, delta: int) -> None:
        if delta <= 0:
            return
        product = self.get_product(product_id)
        if not product:
            return
        current_stock = int(product.get("stock_quantity") or 0)
        self._supabase_request(
            lambda: self._supabase.table("products")
            .update({"stock_quantity": current_stock + delta})
            .eq("id", product_id)
            .execute()
        )

    def create_product(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(lambda: self._supabase.table("products").insert(payload).execute())
        if not result.data:
            raise RuntimeError("Не удалось создать товар")
        return result.data[0]

    def update_product(self, product_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(
            lambda: self._supabase.table("products").update(payload).eq("id", product_id).execute()
        )
        if not result.data:
            raise LookupError("Товар не найден")
        return result.data[0]

    def delete_product(self, product_id: int) -> None:
        self._supabase_request(lambda: self._supabase.table("products").delete().eq("id", product_id).execute())

    def create_category(self, name: str) -> Dict[str, Any]:
        result = self._supabase_request(
            lambda: self._supabase.table("categories").insert({"name": name.strip()}).execute()
        )
        if not result.data:
            raise RuntimeError("Не удалось создать категорию")
        return result.data[0]

    def update_category(self, category_id: int, name: str) -> Dict[str, Any]:
        result = self._supabase_request(
            lambda: self._supabase.table("categories").update({"name": name.strip()}).eq("id", category_id).execute()
        )
        if not result.data:
            raise LookupError("Категория не найдена")
        return result.data[0]

    def delete_category(self, category_id: int) -> None:
        self._supabase_request(lambda: self._supabase.table("categories").delete().eq("id", category_id).execute())

    def get_contractor_by_vk_id(self, vk_id: int) -> Optional[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("contractors").select("*").eq("vk_id", vk_id).limit(1).execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    def get_contractor_by_id(self, contractor_id: int) -> Optional[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("contractors").select("*").eq("id", contractor_id).limit(1).execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    def get_contractor_by_inn(self, inn: str) -> Optional[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("contractors").select("*").eq("inn", sanitize_digits(inn)).limit(1).execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    def get_contractor_by_contract_number(self, contract_number: str) -> Optional[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("contractors")
            .select("*")
            .eq("contract_number", normalize_identifier(contract_number))
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    def find_contractor_for_login(self, identifier: str) -> Optional[Dict[str, Any]]:
        clean = normalize_identifier(identifier)
        digits = sanitize_digits(identifier)
        candidates: List[Dict[str, Any]] = []
        if digits:
            inn_result = self._supabase_request(
                lambda: self._supabase.table("contractors").select("*").eq("inn", digits).limit(1).execute()
            )
            candidates.extend(inn_result.data or [])
        if clean:
            contract_result = self._supabase_request(
                lambda: self._supabase.table("contractors")
                .select("*")
                .eq("contract_number", clean)
                .limit(1)
                .execute()
            )
            candidates.extend(contract_result.data or [])
        for contractor in candidates:
            if contractor:
                return contractor
        return None

    def bind_vk_to_contractor(self, contractor_id: int, vk_id: int) -> Dict[str, Any]:
        contractor = self.get_contractor_by_id(contractor_id)
        if not contractor:
            raise LookupError("Контрагент не найден")
        current_vk_id = contractor.get("vk_id")
        if current_vk_id and int(current_vk_id) != vk_id:
            raise ValueError("Этот договор уже привязан к другому VK-пользователю")
        self.unbind_vk_from_contractor(vk_id)
        payload = {"vk_id": vk_id}
        result = self._supabase_request(
            lambda: self._supabase.table("contractors").update(payload).eq("id", contractor_id).execute()
        )
        if not result.data:
            raise LookupError("Контрагент не найден")
        return result.data[0]

    def unbind_vk_from_contractor(self, vk_id: int) -> None:
        self._supabase_request(
            lambda: self._supabase.table("contractors").update({"vk_id": None}).eq("vk_id", vk_id).execute()
        )

    def list_contractors(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = self._supabase.table("contractors").select("*").order("id", desc=True).limit(limit)
        if status:
            query = query.eq("status", status)
        result = self._supabase_request(lambda: query.execute())
        return result.data or []

    def update_contractor(self, contractor_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(
            lambda: self._supabase.table("contractors").update(payload).eq("id", contractor_id).execute()
        )
        if not result.data:
            raise LookupError("Контрагент не найден")
        return result.data[0]

    def create_contractor(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(lambda: self._supabase.table("contractors").insert(payload).execute())
        if not result.data:
            raise RuntimeError("Не удалось создать контрагента")
        return result.data[0]

    def create_lead_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(lambda: self._supabase.table("lead_requests").insert(payload).execute())
        if not result.data:
            raise RuntimeError("Не удалось сохранить заявку на сотрудничество")
        return result.data[0]

    def list_lead_requests(self, limit: int = 100) -> List[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("lead_requests").select("*").order("id", desc=True).limit(limit).execute()
        )
        return result.data or []

    def get_lead_request_by_id(self, lead_id: int) -> Optional[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("lead_requests").select("*").eq("id", lead_id).limit(1).execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    def update_lead_request(self, lead_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(
            lambda: self._supabase.table("lead_requests").update(payload).eq("id", lead_id).execute()
        )
        if not result.data:
            raise LookupError("Заявка на сотрудничество не найдена")
        return result.data[0]

    def generate_contract_number(self, lead_id: int) -> str:
        base = f"ДГ-{lead_id}-{current_year()}"
        candidate = base
        suffix = 1
        while self.get_contractor_by_contract_number(candidate):
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def create_support_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(lambda: self._supabase.table("support_requests").insert(payload).execute())
        if not result.data:
            raise RuntimeError("Не удалось сохранить обращение")
        return result.data[0]

    def list_support_requests(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = self._supabase.table("support_requests").select("*").order("id", desc=True).limit(limit)
        if status:
            query = query.eq("status", status)
        result = self._supabase_request(lambda: query.execute())
        return result.data or []

    def update_support_request(self, request_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(
            lambda: self._supabase.table("support_requests").update(payload).eq("id", request_id).execute()
        )
        if not result.data:
            raise LookupError("Обращение не найдено")
        return result.data[0]

    def create_document_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(lambda: self._supabase.table("document_requests").insert(payload).execute())
        if not result.data:
            raise RuntimeError("Не удалось сохранить запрос документа")
        return result.data[0]

    def list_document_requests(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = self._supabase.table("document_requests").select("*").order("id", desc=True).limit(limit)
        if status:
            query = query.eq("status", status)
        result = self._supabase_request(lambda: query.execute())
        rows = result.data or []
        for row in rows:
            contractor = self.get_contractor_by_id(int(row.get("contractor_id") or 0)) or {}
            order = self.get_order(int(row.get("order_id") or 0)) if row.get("order_id") else None
            row["contractor"] = contractor_public_view(contractor) if contractor else None
            row["order"] = order_view(order) if order else None
        return rows

    def update_document_request(self, request_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(
            lambda: self._supabase.table("document_requests").update(payload).eq("id", request_id).execute()
        )
        if not result.data:
            raise LookupError("Запрос документа не найден")
        row = result.data[0]
        contractor = self.get_contractor_by_id(int(row.get("contractor_id") or 0)) or {}
        order = self.get_order(int(row.get("order_id") or 0)) if row.get("order_id") else None
        row["contractor"] = contractor_public_view(contractor) if contractor else None
        row["order"] = order_view(order) if order else None
        return row

    def create_contractor_document(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._supabase_request(lambda: self._supabase.table("contractor_documents").insert(payload).execute())
        if not result.data:
            raise RuntimeError("Не удалось сохранить документ")
        return result.data[0]

    def list_contractor_documents(self, contractor_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("contractor_documents")
            .select("*")
            .eq("contractor_id", contractor_id)
            .order("uploaded_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []

    def list_all_contractor_documents(self, limit: int = 200) -> List[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("contractor_documents")
            .select("*")
            .order("uploaded_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = result.data or []
        for row in rows:
            contractor = self.get_contractor_by_id(int(row.get("contractor_id") or 0)) or {}
            order = self.get_order(int(row.get("order_id") or 0)) if row.get("order_id") else None
            row["contractor"] = contractor_public_view(contractor) if contractor else None
            row["order"] = order_view(order) if order else None
        return rows

    def next_customer_order_number(self, contractor_id: int) -> int:
        result = self._supabase_request(
            lambda: self._supabase.table("orders")
            .select("customer_order_number")
            .eq("contractor_id", contractor_id)
            .order("customer_order_number", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        last_value = int(rows[0].get("customer_order_number") or 0) if rows else 0
        return last_value + 1

    def validate_cart(self, items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], float]:
        normalized: List[Dict[str, Any]] = []
        total = 0.0
        if not items:
            raise ValueError("Корзина пуста")
        for raw in items:
            product_id = int(raw.get("product_id") or raw.get("id") or 0)
            quantity = int(raw.get("quantity") or 0)
            if product_id <= 0:
                raise ValueError("Некорректный product_id")
            if quantity <= 0 or quantity > 99999:
                raise ValueError("Некорректное количество товара")
            product = self.get_product(product_id)
            if not product:
                raise ValueError(f"Товар #{product_id} не найден")
            min_quantity = int(product.get("min_quantity") or 1)
            stock_quantity = int(product.get("stock_quantity") or 0)
            if quantity < min_quantity:
                raise ValueError(f"Минимальный квант для «{product.get('name')}» — {min_quantity}")
            if stock_quantity and quantity > stock_quantity:
                raise ValueError("Недостаточно товара на складе")
            price = money(product.get("price"))
            line_total = money(price * quantity)
            total += line_total
            normalized.append(
                {
                    "product_id": product_id,
                    "name": str(product.get("name") or f"Товар #{product_id}"),
                    "description": product.get("description"),
                    "price": price,
                    "quantity": quantity,
                    "line_total": line_total,
                    "min_quantity": min_quantity,
                    "stock_quantity": stock_quantity,
                }
            )
        return normalized, money(total)

    def reserve_stock_for_items(self, items: List[Dict[str, Any]], max_retries: int = 3) -> List[Dict[str, int]]:
        tracked_items = [item for item in items if int(item.get("stock_quantity") or 0) > 0]
        if not tracked_items:
            return []

        for attempt in range(max_retries):
            reserved: List[Dict[str, int]] = []
            try:
                for item in tracked_items:
                    product_id = int(item["product_id"])
                    quantity = int(item["quantity"])
                    product = self.get_product(product_id)
                    if not product:
                        raise ValueError("Недостаточно товара на складе")
                    current_stock = int(product.get("stock_quantity") or 0)
                    if quantity > current_stock:
                        raise ValueError("Недостаточно товара на складе")
                    updated = self.update_product_stock_if_matches(product_id, current_stock, current_stock - quantity)
                    if not updated:
                        raise RuntimeError("stock_conflict")
                    reserved.append({"product_id": product_id, "quantity": quantity})
                return reserved
            except ValueError:
                for reservation in reserved:
                    self.increment_product_stock(int(reservation["product_id"]), int(reservation["quantity"]))
                raise
            except RuntimeError as exc:
                for reservation in reserved:
                    self.increment_product_stock(int(reservation["product_id"]), int(reservation["quantity"]))
                if str(exc) != "stock_conflict" or attempt == max_retries - 1:
                    raise ValueError("Недостаточно товара на складе")
                time.sleep(0.1 * (attempt + 1))

        raise ValueError("Недостаточно товара на складе")

    def release_reserved_stock(self, reserved: List[Dict[str, int]]) -> None:
        for reservation in reserved:
            self.increment_product_stock(int(reservation["product_id"]), int(reservation["quantity"]))

    def list_order_items(self, order_id: int) -> List[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("order_items")
            .select("id,order_id,product_id,quantity,price")
            .eq("order_id", order_id)
            .order("id")
            .execute()
        )
        rows = result.data or []
        items: List[Dict[str, Any]] = []
        for row in rows:
            product = self.get_product(int(row.get("product_id") or 0)) or {}
            quantity = int(row.get("quantity") or 0)
            price = money(row.get("price"))
            items.append(
                {
                    "id": row.get("id"),
                    "order_id": row.get("order_id"),
                    "product_id": row.get("product_id"),
                    "name": product.get("name") or f"Товар #{row.get('product_id')}",
                    "quantity": quantity,
                    "price": price,
                    "line_total": money(price * quantity),
                }
            )
        return items

    def get_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("orders").select("*").eq("id", order_id).limit(1).execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    def get_order_for_vk_user(self, vk_id: int, order_id: int) -> Optional[Dict[str, Any]]:
        contractor = self.get_contractor_by_vk_id(vk_id)
        if not contractor:
            return None
        result = self._supabase_request(
            lambda: self._supabase.table("orders")
            .select("*")
            .eq("id", order_id)
            .eq("contractor_id", contractor["id"])
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    def attach_invoice(self, order_id: int, invoice_path: str) -> Dict[str, Any]:
        result = self._supabase_request(
            lambda: self._supabase.table("orders")
            .update({"invoice_pdf_url": invoice_path, "updated_at": iso_now()})
            .eq("id", order_id)
            .execute()
        )
        if not result.data:
            raise LookupError("Заказ не найден")
        return result.data[0]

    def update_order_fields(self, order_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = {**payload, "updated_at": iso_now()}
        result = self._supabase_request(
            lambda: self._supabase.table("orders").update(payload).eq("id", order_id).execute()
        )
        if not result.data:
            raise LookupError("Заказ не найден")
        return result.data[0]

    def change_contractor_debt(self, contractor_id: int, delta: float) -> Dict[str, Any]:
        contractor = self.get_contractor_by_id(contractor_id)
        if not contractor:
            raise LookupError("Контрагент не найден")
        current_debt = money(contractor.get("current_debt"))
        new_debt = max(0.0, money(current_debt + delta))
        return self.update_contractor(contractor_id, {"current_debt": new_debt})

    def list_orders_for_contractor(self, contractor_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        result = self._supabase_request(
            lambda: self._supabase.table("orders")
            .select("*")
            .eq("contractor_id", contractor_id)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        rows = result.data or []
        for row in rows:
            row["items"] = self.list_order_items(int(row["id"]))
        return rows

    def list_all_orders(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = self._supabase.table("orders").select("*").order("id", desc=True).limit(limit)
        if status:
            query = query.eq("status", status)
        result = self._supabase_request(lambda: query.execute())
        rows = result.data or []
        for row in rows:
            contractor = self.get_contractor_by_id(int(row["contractor_id"])) or {}
            row["contractor"] = {
                "id": contractor.get("id"),
                "company_name": contractor.get("company_name"),
                "inn": contractor.get("inn"),
                "contract_number": contractor.get("contract_number"),
            }
            row["items"] = self.list_order_items(int(row["id"]))
        return rows


    def create_b2b_order(self, vk_id: int, items: List[Dict[str, Any]], comment: Optional[str]) -> Dict[str, Any]:
        contractor = self.get_contractor_by_vk_id(vk_id)
        if not contractor:
            raise PermissionError("Контрагент не авторизован")
        if contractor.get("status") == "blocked":
            raise PermissionError("Контрагент заблокирован")

        normalized_items, total_amount = self.validate_cart(items)
        payment_type = contractor.get("payment_type") or "prepayment"
        if payment_type not in PAYMENT_TYPES:
            payment_type = "prepayment"

        current_debt = money(contractor.get("current_debt"))
        credit_limit = money(contractor.get("credit_limit"))
        payment_days = int(contractor.get("payment_days") or 0)
        payment_due_date = None
        status = "waiting_payment"

        if payment_type == "deferred":
            if current_debt + total_amount > credit_limit:
                available = max(0.0, money(credit_limit - current_debt))
                raise ValueError(
                    f"Лимит превышен. Доступно к отгрузке на {available:.2f} ₽. Погасите задолженность."
                )
            payment_due_date = (utcnow() + timedelta(days=payment_days)).date().isoformat()
            status = "confirmed"

        reserved_stock = self.reserve_stock_for_items(normalized_items)
        try:
            customer_order_number = self.next_customer_order_number(int(contractor["id"]))
            order_payload = {
                "contractor_id": contractor["id"],
                "status": status,
                "total_amount": total_amount,
                "payment_type": payment_type,
                "payment_due_date": payment_due_date,
                "payment_notified_5d": False,
                "payment_notified_1d": False,
                "invoice_pdf_url": None,
                "order_number": None,
                "customer_order_number": customer_order_number,
                "reconciliation_requested": False,
                "duplicate_invoice_requested": False,
                "payment_confirmed_at": None,
                "shipped_at": None,
                "completed_at": None,
                "comment": (comment or "").strip() or None,
                "created_at": iso_now(),
                "updated_at": iso_now(),
            }
            order_result = self._supabase_request(
                lambda: self._supabase.table("orders").insert(order_payload).execute()
            )
            if not order_result.data:
                raise RuntimeError("Не удалось создать заявку")
            order = order_result.data[0]
            order = self.update_order_fields(int(order["id"]), {"order_number": format_order_number(int(order["id"]))})

            order_items_payload = [
                {
                    "order_id": order["id"],
                    "product_id": item["product_id"],
                    "quantity": item["quantity"],
                    "price": item["price"],
                }
                for item in normalized_items
            ]
            self._supabase_request(lambda: self._supabase.table("order_items").insert(order_items_payload).execute())

            if payment_type == "deferred":
                self.change_contractor_debt(int(contractor["id"]), total_amount)
            else:
                try:
                    invoice_path = self.generate_invoice_pdf(order, contractor, normalized_items)
                    order = self.attach_invoice(int(order["id"]), invoice_path)
                    self.create_contractor_document(
                        {
                            "contractor_id": contractor["id"],
                            "order_id": order["id"],
                            "document_type": "invoice",
                            "title": f"Счёт по заявке {order.get('order_number') or format_order_number(int(order['id']))}",
                            "file_name": Path(invoice_path).name,
                            "file_url": invoice_path,
                            "status": "active",
                            "uploaded_at": iso_now(),
                        }
                    )
                except Exception:
                    logger.exception("invoice generation failed for order %s", order.get("id"))
                    order["invoice_pdf_url"] = None

            order["items"] = normalized_items
            order["contractor"] = contractor
            return order
        except Exception:
            self.release_reserved_stock(reserved_stock)
            raise

    def generate_invoice_pdf(
        self,
        order: Dict[str, Any],
        contractor: Dict[str, Any],
        items: List[Dict[str, Any]],
    ) -> str:
        invoice_path = INVOICE_DIR / f"invoice_order_{order['id']}.pdf"
        invoice_path.write_bytes(build_invoice_pdf(order, contractor, items))
        invoice_url = f"/documents/invoices/{invoice_path.name}"
        if BACKEND_PUBLIC_URL:
            return f"{BACKEND_PUBLIC_URL}{invoice_url}"
        return invoice_url

    def notify_payment_by_partner(self, vk_id: int, order_id: int) -> Dict[str, Any]:
        order = self.get_order_for_vk_user(vk_id, order_id)
        if not order:
            raise LookupError("Заявка не найдена")
        if order.get("payment_type") != "prepayment":
            raise ValueError("Кнопка доступна только для предоплаты")
        if order.get("status") not in {"waiting_payment", "payment_check"}:
            raise ValueError("Для этой заявки подтверждение оплаты уже не требуется")
        return self.update_order_fields(order_id, {"status": "payment_check"})

    def build_balance_payload(self, vk_id: int) -> Dict[str, Any]:
        contractor = self.get_contractor_by_vk_id(vk_id)
        if not contractor:
            raise PermissionError("Контрагент не авторизован")
        orders = self.list_orders_for_contractor(int(contractor["id"]), limit=10)
        deferred_orders = [order for order in orders if order.get("payment_due_date")]
        nearest_due = None
        for order in deferred_orders:
            dt = parse_dt(order.get("payment_due_date")) or parse_dt(f"{order.get('payment_due_date')}T00:00:00+00:00")
            if not dt:
                continue
            if nearest_due is None or dt < nearest_due:
                nearest_due = dt
        current_debt = money(contractor.get("current_debt"))
        credit_limit = money(contractor.get("credit_limit"))
        return {
            "company_name": contractor.get("company_name"),
            "contract_number": contractor.get("contract_number"),
            "payment_type": contractor.get("payment_type") or "prepayment",
            "current_debt": current_debt,
            "credit_limit": credit_limit,
            "available_limit": max(0.0, money(credit_limit - current_debt)),
            "nearest_due_date": nearest_due.date().isoformat() if nearest_due else None,
            "orders": orders,
            "contractor": contractor,
        }

    def get_order_stats(self) -> Dict[str, Any]:
        orders = self._supabase_request(lambda: self._supabase.table("orders").select("*").execute()).data or []
        contractors = self._supabase_request(lambda: self._supabase.table("contractors").select("*").execute()).data or []
        leads = self._supabase_request(lambda: self._supabase.table("lead_requests").select("*").execute()).data or []
        total_amount = sum(money(order.get("total_amount")) for order in orders)
        by_status: Dict[str, int] = {}
        for order in orders:
            status = str(order.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        active_contractors = sum(1 for contractor in contractors if contractor.get("status") == "active")
        overdue_orders = 0
        now_date = utcnow().date()
        for order in orders:
            due_raw = order.get("payment_due_date")
            if not due_raw or order.get("status") in PAID_ORDER_STATUSES:
                continue
            try:
                due_date = datetime.fromisoformat(str(due_raw)).date() if "T" in str(due_raw) else datetime.strptime(str(due_raw), "%Y-%m-%d").date()
            except ValueError:
                continue
            if due_date < now_date:
                overdue_orders += 1
        return {
            "total_orders": len(orders),
            "total_amount": money(total_amount),
            "active_contractors": active_contractors,
            "lead_requests": len(leads),
            "overdue_orders": overdue_orders,
            "by_status": by_status,
        }

    def list_payment_reminders(self) -> List[Dict[str, Any]]:
        orders = self._supabase_request(lambda: self._supabase.table("orders").select("*").execute()).data or []
        reminders: List[Dict[str, Any]] = []
        today = utcnow().date()
        for order in orders:
            if order.get("payment_type") != "deferred":
                continue
            if order.get("status") in PAID_ORDER_STATUSES or not order.get("payment_due_date"):
                continue
            due_text = str(order["payment_due_date"])
            try:
                due_date = datetime.fromisoformat(due_text).date() if "T" in due_text else datetime.strptime(due_text, "%Y-%m-%d").date()
            except ValueError:
                continue
            days_left = (due_date - today).days
            if days_left == 5 and not order.get("payment_notified_5d"):
                reminders.append({"order_id": order["id"], "days_left": 5, "field": "payment_notified_5d"})
            if days_left == 1 and not order.get("payment_notified_1d"):
                reminders.append({"order_id": order["id"], "days_left": 1, "field": "payment_notified_1d"})
        return reminders

    def apply_payment_reminders(self) -> List[Dict[str, Any]]:
        reminders = self.list_payment_reminders()
        applied: List[Dict[str, Any]] = []
        for reminder in reminders:
            order_id = int(reminder["order_id"])
            self.update_order_fields(order_id, {reminder["field"]: True})
            order = self.get_order(order_id) or {}
            contractor = self.get_contractor_by_id(int(order.get("contractor_id") or 0)) or {}
            applied.append(
                {
                    "order_id": order_id,
                    "days_left": reminder["days_left"],
                    "company_name": contractor.get("company_name"),
                    "vk_id": contractor.get("vk_id"),
                    "message": f"Напоминание: до оплаты заявки #{order_id} осталось {reminder['days_left']} дн.",
                }
            )
        return applied


repo = Repo(SUPABASE_URL, SUPABASE_KEY)
pending_support_requests: Dict[int, bool] = {}

app = FastAPI(title="RAIPO B2B VK System", version="2.0.0")
allow_origins = [item.strip() for item in CORS_ALLOW_ORIGINS.split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins if allow_origins != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CartItemIn(BaseModel):
    product_id: int = Field(..., ge=1)
    quantity: int = Field(..., ge=1, le=99999)


class CartCheckIn(BaseModel):
    items: List[CartItemIn] = Field(default_factory=list)


class AuthLinkIn(BaseModel):
    vk_id: int = Field(..., ge=1)
    identifier: str = Field(..., min_length=3, max_length=64)


class LogoutIn(BaseModel):
    vk_id: int = Field(..., ge=1)


class LeadRequestIn(BaseModel):
    vk_id: Optional[int] = Field(default=None, ge=1)
    company_name: str = Field(..., min_length=2, max_length=200)
    inn: str = Field(..., min_length=10, max_length=20)
    contact_name: str = Field(..., min_length=2, max_length=120)
    phone: str = Field(..., min_length=5, max_length=40)
    city: str = Field(..., min_length=2, max_length=120)


class B2BOrderCreateIn(BaseModel):
    vk_id: int = Field(..., ge=1)
    items: List[CartItemIn] = Field(default_factory=list)
    comment: Optional[str] = Field(default=None, max_length=1000)


class AdminOrderStatusPatchIn(BaseModel):
    status: str = Field(..., min_length=2, max_length=40)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in ORDER_STATUSES:
            raise ValueError("Некорректный статус заявки")
        return value


class ContractorUpdateIn(BaseModel):
    company_name: Optional[str] = Field(default=None, max_length=200)
    inn: Optional[str] = Field(default=None, max_length=20)
    contract_number: Optional[str] = Field(default=None, max_length=50)
    payment_type: Optional[str] = Field(default=None, max_length=20)
    credit_limit: Optional[float] = Field(default=None, ge=0)
    current_debt: Optional[float] = Field(default=None, ge=0)
    payment_days: Optional[int] = Field(default=None, ge=0, le=365)
    email: Optional[str] = Field(default=None, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=40)
    status: Optional[str] = Field(default=None, max_length=20)
    vk_id: Optional[int] = Field(default=None, ge=1)

    @field_validator("payment_type")
    @classmethod
    def validate_payment_type(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in PAYMENT_TYPES:
            raise ValueError("Некорректный тип оплаты")
        return value

    @field_validator("status")
    @classmethod
    def validate_contractor_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in CONTRACTOR_STATUSES:
            raise ValueError("Некорректный статус контрагента")
        return value


class ProductCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    price: float = Field(..., ge=0)
    min_quantity: int = Field(default=1, ge=1)
    stock_quantity: int = Field(default=0, ge=0)
    category_id: int = Field(..., ge=1)
    image: Optional[str] = Field(default=None, max_length=500)


class ProductUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    price: Optional[float] = Field(default=None, ge=0)
    min_quantity: Optional[int] = Field(default=None, ge=1)
    stock_quantity: Optional[int] = Field(default=None, ge=0)
    category_id: Optional[int] = Field(default=None, ge=1)
    image: Optional[str] = Field(default=None, max_length=500)


class CategoryIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class DuplicateDocumentIn(BaseModel):
    vk_id: int = Field(..., ge=1)
    order_id: int = Field(..., ge=1)


class SupportRequestIn(BaseModel):
    vk_id: int = Field(..., ge=1)
    subject: str = Field(..., min_length=2, max_length=200)
    message: str = Field(..., min_length=3, max_length=4000)


class SupportRequestStatusIn(BaseModel):
    status: str = Field(..., min_length=2, max_length=30)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"new", "in_progress", "closed"}:
            raise ValueError("Некорректный статус обращения")
        return value


class DocumentRequestStatusIn(BaseModel):
    status: str = Field(..., min_length=2, max_length=30)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in DOCUMENT_REQUEST_STATUSES:
            raise ValueError("Некорректный статус запроса документа")
        return value


class AdminDocumentUploadIn(BaseModel):
    contractor_id: int = Field(..., ge=1)
    document_type: str = Field(..., min_length=2, max_length=40)
    title: str = Field(..., min_length=1, max_length=200)
    order_id: Optional[int] = Field(default=None, ge=1)
    request_id: Optional[int] = Field(default=None, ge=1)
    file_name: str = Field(..., min_length=1, max_length=255)
    file_content_base64: str = Field(..., min_length=4)

    @field_validator("document_type")
    @classmethod
    def validate_document_type(cls, value: str) -> str:
        if value not in {"invoice", "waybill", "contract", "reconciliation_act", "other"}:
            raise ValueError("Некорректный тип документа")
        return value


def contractor_public_view(contractor: Dict[str, Any]) -> Dict[str, Any]:
    current_debt = money(contractor.get("current_debt"))
    credit_limit = money(contractor.get("credit_limit"))
    return {
        "id": contractor.get("id"),
        "vk_id": contractor.get("vk_id"),
        "company_name": contractor.get("company_name"),
        "inn": contractor.get("inn"),
        "contract_number": contractor.get("contract_number"),
        "payment_type": contractor.get("payment_type") or "prepayment",
        "payment_type_label": PAYMENT_TYPE_LABELS.get(contractor.get("payment_type") or "prepayment"),
        "credit_limit": credit_limit,
        "current_debt": current_debt,
        "available_limit": max(0.0, money(credit_limit - current_debt)),
        "payment_days": int(contractor.get("payment_days") or 0),
        "email": contractor.get("email"),
        "phone": contractor.get("phone"),
        "status": contractor.get("status"),
        "status_label": CONTRACTOR_STATUS_LABELS.get(contractor.get("status"), contractor.get("status")),
        "created_at": contractor.get("created_at"),
    }


def order_view(order: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(order)
    result["total_amount"] = money(order.get("total_amount"))
    result["status_label"] = ORDER_STATUS_LABELS.get(order.get("status"), order.get("status"))
    result["payment_type_label"] = PAYMENT_TYPE_LABELS.get(order.get("payment_type"), order.get("payment_type"))
    result["order_number"] = order.get("order_number") or format_order_number(int(order.get("id") or 0))
    result["customer_order_number"] = order.get("customer_order_number")
    result["customer_order_number_label"] = format_customer_order_number(order.get("customer_order_number"))
    return result


def contractor_document_view(document: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(document)
    result["document_type_label"] = {
        "invoice": "Счёт",
        "waybill": "Накладная",
        "contract": "Договор",
        "reconciliation_act": "Акт сверки",
        "other": "Документ",
    }.get(document.get("document_type"), document.get("document_type") or "Документ")
    return result


def is_vk_admin(vk_id: int) -> bool:
    if vk_id <= 0 or VK_GROUP_ID <= 0:
        return False
    try:
        session = vk_api.VkApi(token=VK_TOKEN)
        vk = session.get_api()
        result = vk.groups.getMembers(group_id=VK_GROUP_ID, filter="managers", fields="")
        for item in result.get("items") or []:
            if int(item.get("id", 0)) == vk_id and item.get("role", "").lower() in ADMIN_ROLES:
                return True
    except Exception:
        logger.exception("VK admin check failed vk_id=%s", vk_id)
    return False


def get_admin_vk_ids() -> List[int]:
    if VK_GROUP_ID <= 0:
        return []
    try:
        session = vk_api.VkApi(token=VK_TOKEN)
        vk = session.get_api()
        result = vk.groups.getMembers(group_id=VK_GROUP_ID, filter="managers", fields="")
        admin_ids: List[int] = []
        for item in result.get("items") or []:
            if item.get("role", "").lower() in ADMIN_ROLES:
                user_id = int(item.get("id") or 0)
                if user_id > 0:
                    admin_ids.append(user_id)
        return admin_ids
    except Exception:
        logger.exception("Failed to load VK admins")
        return []


def ensure_admin(vk_id: int) -> None:
    if not is_vk_admin(vk_id):
        raise HTTPException(403, "Доступ разрешён только администратору сообщества VK")


def ensure_partner(vk_id: int) -> Dict[str, Any]:
    if is_vk_admin(vk_id):
        raise HTTPException(403, "Раздел доступен только контрагентам")
    contractor = repo.get_contractor_by_vk_id(vk_id)
    if not contractor:
        raise HTTPException(403, "Контрагент не авторизован")
    return contractor


def build_session(vk_id: int) -> Dict[str, Any]:
    contractor = repo.get_contractor_by_vk_id(vk_id)
    admin = is_vk_admin(vk_id)
    if admin:
        role = "admin"
    elif contractor:
        role = "partner"
    else:
        role = "guest"
    return {
        "vk_id": vk_id,
        "role": role,
        "is_admin": admin,
        "contractor": contractor_public_view(contractor) if contractor else None,
        "mini_app_url": MINI_APP_URL,
    }


def open_app_button(label: str = "Открыть кабинет") -> Dict[str, Any]:
    if VK_APP_ID and VK_APP_OWNER_ID:
        return {
            "action": {
                "type": "open_app",
                "app_id": VK_APP_ID,
                "owner_id": VK_APP_OWNER_ID,
                "hash": VK_APP_HASH or "",
                "label": label,
            }
        }
    if MINI_APP_URL:
        return {
            "action": {
                "type": "open_link",
                "link": MINI_APP_URL,
                "label": label,
            }
        }
    return {
        "action": {
            "type": "text",
            "label": label,
            "payload": json.dumps({"cmd": "menu"}, ensure_ascii=False),
        },
        "color": "primary",
    }


def text_button(label: str, cmd: str, color: str = "secondary") -> Dict[str, Any]:
    return {
        "action": {"type": "text", "label": label, "payload": json.dumps({"cmd": cmd}, ensure_ascii=False)},
        "color": color,
    }


def keyboard(rows: List[List[Dict[str, Any]]]) -> str:
    return json.dumps({"one_time": False, "inline": False, "buttons": rows}, ensure_ascii=False)


def guest_keyboard() -> str:
    return keyboard(
        [
            [open_app_button("Открыть кабинет")],
            [text_button("О компании", "about"), text_button("Контакты", "contacts")],
            [text_button("Ассортимент", "catalog"), text_button("Авторизация", "auth_help", "primary")],
            [text_button("Помощь", "help"), text_button("Сотрудничество", "lead", "positive")],
            [text_button("Связаться с менеджером", "support")],
        ]
    )


def partner_keyboard() -> str:
    return keyboard(
        [
            [open_app_button("Каталог и заявки")],
            [text_button("Мой баланс", "balance", "primary"), text_button("История заявок", "history", "primary")],
            [text_button("Документы", "documents"), text_button("Помощь", "help")],
            [text_button("Оплата", "payment_help"), text_button("Связаться с менеджером", "support")],
            [text_button("Сменить организацию", "logout")],
            [text_button("Контакты", "contacts"), text_button("О компании", "about")],
        ]
    )


def admin_keyboard() -> str:
    return keyboard(
        [
            [open_app_button("Открыть управление")],
            [text_button("Статистика", "admin_stats", "primary"), text_button("Заявки", "history", "primary")],
            [text_button("Контрагенты", "contractors"), text_button("Заявки на сотрудничество", "leads")],
            [text_button("Обращения", "support_list"), text_button("Напоминания", "reminders")],
            [text_button("Контакты", "contacts"), text_button("Помощь", "help")],
        ]
    )


def keyboard_for_role(vk_id: int) -> str:
    session = build_session(vk_id)
    if session["role"] == "admin":
        return admin_keyboard()
    if session["role"] == "partner":
        return partner_keyboard()
    return guest_keyboard()


def safe_send(vk: Any, user_id: int, message: str, kb: Optional[str] = None) -> None:
    params = {"user_id": user_id, "message": message, "random_id": get_random_id()}
    if kb:
        params["keyboard"] = kb
    try:
        vk.messages.send(**params)
    except ApiError as exc:
        if getattr(exc, "code", None) == 911 and kb:
            params.pop("keyboard", None)
            vk.messages.send(**params)
        logger.exception("VK send failed")


def send_vk_notification(user_id: int, message: str) -> bool:
    if user_id <= 0:
        return False
    try:
        session = vk_api.VkApi(token=VK_TOKEN)
        vk = session.get_api()
        safe_send(vk, user_id, message)
        return True
    except Exception:
        logger.exception("VK notification failed user_id=%s", user_id)
        return False


def notify_admins(message: str) -> None:
    for admin_vk_id in get_admin_vk_ids():
        if not send_vk_notification(admin_vk_id, message):
            logger.warning("Admin notification failed admin_vk_id=%s", admin_vk_id)


def show_menu(vk: Any, user_id: int) -> None:
    session = build_session(user_id)
    if session["role"] == "admin":
        message = (
            "Система заявок «Слободское РАЙПО».\n\n"
            "Вы определены как администратор сообщества VK. В кабинете доступны заявки, контрагенты, заявки на сотрудничество и статистика."
        )
    elif session["role"] == "partner":
        contractor = session["contractor"] or {}
        message = (
            f"Личный кабинет контрагента активен.\n\n"
            f"Организация: {contractor.get('company_name')}\n"
            f"Договор: {contractor.get('contract_number') or 'не указан'}\n"
            f"Тип оплаты: {contractor.get('payment_type_label')}\n"
            f"Задолженность: {money(contractor.get('current_debt')):.2f} ₽"
        )
    else:
        message = (
            "Добро пожаловать в личный кабинет «Слободское РАЙПО».\n\n"
            "Здесь можно ознакомиться с ассортиментом, оставить заявку на сотрудничество и авторизоваться по ИНН или номеру договора."
        )
    safe_send(vk, user_id, message, kb=keyboard_for_role(user_id))


def handle_about(vk: Any, user_id: int) -> None:
    safe_send(
        vk,
        user_id,
        "Слободское РАЙПО — локальный поставщик продовольственных товаров и продукции для организаций.\n"
        "В кабинете доступны работа по договорам поставки, счета, отсрочки и сопровождение заявок.",
        kb=keyboard_for_role(user_id),
    )


def handle_contacts(vk: Any, user_id: int) -> None:
    safe_send(
        vk,
        user_id,
        "Контакты отдела продаж:\n"
        "Телефон: +7 (83362) 4-00-00\n"
        "Email: b2b@raipo.local\n"
        "Адрес: г. Слободской, Кировская область",
        kb=keyboard_for_role(user_id),
    )


def handle_catalog(vk: Any, user_id: int) -> None:
    products = repo.list_products()[:5]
    if not products:
        safe_send(vk, user_id, "Каталог пока пуст.", kb=keyboard_for_role(user_id))
        return
    lines = ["Краткий ассортимент:"]
    for product in products:
        lines.append(
            f"• {product.get('name')} — {money(product.get('price')):.2f} ₽, "
            f"мин. квант {int(product.get('min_quantity') or 1)}"
        )
    lines.append("\nПолный каталог доступен в кабинете.")
    safe_send(vk, user_id, "\n".join(lines), kb=keyboard_for_role(user_id))


def handle_auth_help(vk: Any, user_id: int) -> None:
    safe_send(
        vk,
        user_id,
        "Для входа откройте кабинет и введите ИНН организации или номер договора.\n"
        "Если контрагент уже привязан к вашему VK ID, вход выполнится автоматически.\n"
        "Также можно прислать сюда ИНН или номер договора одним сообщением.",
        kb=keyboard_for_role(user_id),
    )


def handle_lead(vk: Any, user_id: int) -> None:
    safe_send(
        vk,
        user_id,
        "Заявка на сотрудничество подаётся через кабинет: укажите компанию, ИНН, контактное лицо, телефон и город.",
        kb=keyboard_for_role(user_id),
    )


def handle_balance(vk: Any, user_id: int) -> None:
    try:
        data = repo.build_balance_payload(user_id)
    except PermissionError:
        safe_send(vk, user_id, "Сначала авторизуйтесь как контрагент.", kb=keyboard_for_role(user_id))
        return
    lines = [
        f"Организация: {data['company_name']}",
        f"Договор: {data['contract_number'] or 'не указан'}",
        f"Тип оплаты: {PAYMENT_TYPE_LABELS.get(data['payment_type'], data['payment_type'])}",
        f"Задолженность: {data['current_debt']:.2f} ₽",
        f"Кредитный лимит: {data['credit_limit']:.2f} ₽",
        f"Доступный остаток: {data['available_limit']:.2f} ₽",
    ]
    if data["nearest_due_date"]:
        lines.append(f"Ближайший срок оплаты: {data['nearest_due_date']}")
    safe_send(vk, user_id, "\n".join(lines), kb=keyboard_for_role(user_id))


def handle_history(vk: Any, user_id: int) -> None:
    session = build_session(user_id)
    if session["role"] == "partner":
        contractor = repo.get_contractor_by_vk_id(user_id)
        orders = repo.list_orders_for_contractor(int(contractor["id"]), limit=10) if contractor else []
    elif session["role"] == "admin":
        orders = repo.list_all_orders(limit=10)
    else:
        orders = []

    if not orders:
        safe_send(vk, user_id, "История заявок пока пуста.", kb=keyboard_for_role(user_id))
        return

    lines = ["Последние заявки:"]
    for order in orders[:10]:
        order_number = order.get("order_number") or format_order_number(int(order.get("id") or 0))
        customer_number = format_customer_order_number(order.get("customer_order_number"))
        status_label = ORDER_STATUS_LABELS.get(order.get("status"), order.get("status"))
        if session["role"] == "admin":
            prefix = order.get("contractor", {}).get("company_name") or "Контрагент"
            lines.append(
                f"{order_number} / {prefix} / номер у клиента {customer_number} / "
                f"{status_label} / {money(order.get('total_amount')):.2f} ₽"
            )
        else:
            lines.append(
                f"Ваш заказ {customer_number}\n{order_number}\nСтатус: {status_label}\n"
                f"Сумма: {money(order.get('total_amount')):.2f} ₽"
            )
    safe_send(vk, user_id, "\n".join(lines), kb=keyboard_for_role(user_id))


def handle_documents(vk: Any, user_id: int) -> None:
    safe_send(
        vk,
        user_id,
        "Документы доступны в кабинете:\n"
        "• PDF-счета для предоплаты\n"
        "• акт сверки\n"
        "• запрос дубликата накладной",
        kb=keyboard_for_role(user_id),
    )


def handle_help(vk: Any, user_id: int) -> None:
    safe_send(
        vk,
        user_id,
        "Справка по системе:\n"
        "• Как оформить заказ — выберите товары в каталоге, добавьте их в корзину и подтвердите заявку.\n"
        "• Как получить счёт — после оформления заказа формируется PDF-счёт.\n"
        "• Как узнать статус — раздел «Заявки» в личном кабинете.\n"
        "• Как стать контрагентом — заполните заявку на сотрудничество и дождитесь одобрения.\n"
        "• Как запросить документы — используйте раздел «Документы».",
        kb=keyboard_for_role(user_id),
    )


def handle_payment_help(vk: Any, user_id: int) -> None:
    safe_send(
        vk,
        user_id,
        "Оплата и условия работы:\n"
        "• Предоплата — после оформления заявки формируется счёт, статус меняется после подтверждения оплаты.\n"
        "• Отсрочка — доступна контрагентам с одобренным лимитом.\n"
        "• Лимиты — при отсрочке сумма заявки проверяется относительно доступного остатка.",
        kb=keyboard_for_role(user_id),
    )


def handle_cooperation_help(vk: Any, user_id: int) -> None:
    safe_send(
        vk,
        user_id,
        "Чтобы стать контрагентом, заполните заявку на сотрудничество в кабинете.\n"
        "После одобрения администратором вы получите номер договора и сможете войти по ИНН или номеру договора.",
        kb=keyboard_for_role(user_id),
    )


def handle_support_prompt(vk: Any, user_id: int) -> None:
    pending_support_requests[user_id] = True
    safe_send(vk, user_id, "Напишите ваш вопрос одним сообщением.", kb=keyboard_for_role(user_id))


def handle_support_list(vk: Any, user_id: int) -> None:
    if not is_vk_admin(user_id):
        safe_send(vk, user_id, "Раздел доступен только администратору.", kb=keyboard_for_role(user_id))
        return
    requests = repo.list_support_requests(limit=5)
    if not requests:
        safe_send(vk, user_id, "Обращений пока нет.", kb=keyboard_for_role(user_id))
        return
    lines = ["Последние обращения:"]
    for item in requests:
        lines.append(f"• #{item.get('id')} / VK {item.get('vk_id')} / {item.get('subject')} / {item.get('status')}")
    safe_send(vk, user_id, "\n".join(lines), kb=keyboard_for_role(user_id))


def handle_logout(vk: Any, user_id: int) -> None:
    repo.unbind_vk_from_contractor(user_id)
    safe_send(vk, user_id, "Привязка к организации снята. Можно авторизоваться заново под другим договором.", kb=guest_keyboard())


def handle_admin_stats(vk: Any, user_id: int) -> None:
    if not is_vk_admin(user_id):
        safe_send(vk, user_id, "Команда доступна только администратору.", kb=keyboard_for_role(user_id))
        return
    stats = repo.get_order_stats()
    safe_send(
        vk,
        user_id,
        "Статистика:\n"
        f"Заявок: {stats['total_orders']}\n"
        f"Сумма: {stats['total_amount']:.2f} ₽\n"
        f"Активных контрагентов: {stats['active_contractors']}\n"
        f"Лидов: {stats['lead_requests']}\n"
        f"Просроченных заявок: {stats['overdue_orders']}",
        kb=keyboard_for_role(user_id),
    )


def handle_contractors(vk: Any, user_id: int) -> None:
    if not is_vk_admin(user_id):
        safe_send(vk, user_id, "Команда доступна только администратору.", kb=keyboard_for_role(user_id))
        return
    contractors = repo.list_contractors(limit=5)
    if not contractors:
        safe_send(vk, user_id, "Контрагенты не найдены.", kb=keyboard_for_role(user_id))
        return
    lines = ["Последние контрагенты:"]
    for contractor in contractors:
        lines.append(
            f"• {contractor.get('company_name')} / {contractor.get('contract_number') or '-'} / "
            f"{CONTRACTOR_STATUS_LABELS.get(contractor.get('status'), contractor.get('status'))}"
        )
    safe_send(vk, user_id, "\n".join(lines), kb=keyboard_for_role(user_id))


def handle_leads(vk: Any, user_id: int) -> None:
    if not is_vk_admin(user_id):
        safe_send(vk, user_id, "Команда доступна только администратору.", kb=keyboard_for_role(user_id))
        return
    leads = repo.list_lead_requests(limit=5)
    if not leads:
        safe_send(vk, user_id, "Заявок на сотрудничество пока нет.", kb=keyboard_for_role(user_id))
        return
    lines = ["Последние заявки на сотрудничество:"]
    for lead in leads:
        lines.append(f"• {lead.get('company_name')} / ИНН {lead.get('inn')} / {lead.get('city')}")
    safe_send(vk, user_id, "\n".join(lines), kb=keyboard_for_role(user_id))


def handle_reminders(vk: Any, user_id: int) -> None:
    if not is_vk_admin(user_id):
        safe_send(vk, user_id, "Команда доступна только администратору.", kb=keyboard_for_role(user_id))
        return
    reminders = repo.list_payment_reminders()
    if not reminders:
        safe_send(vk, user_id, "Сейчас нет напоминаний к отправке.", kb=keyboard_for_role(user_id))
        return
    lines = ["Подготовленные напоминания:"]
    for reminder in reminders:
        lines.append(f"• Заказ #{reminder['order_id']} — осталось {reminder['days_left']} дн.")
    safe_send(vk, user_id, "\n".join(lines), kb=keyboard_for_role(user_id))


def try_bind_from_message(vk: Any, user_id: int, text: str) -> bool:
    normalized = normalize_identifier(text)
    digits = sanitize_digits(text)
    if not normalized and not digits:
        return False

    contractor = repo.find_contractor_for_login(text)
    if not contractor:
        return False
    if contractor.get("status") == "blocked":
        safe_send(vk, user_id, "Контрагент найден, но сейчас заблокирован. Свяжитесь с администратором.", kb=guest_keyboard())
        return True
    try:
        contractor = repo.bind_vk_to_contractor(int(contractor["id"]), user_id)
    except ValueError as exc:
        safe_send(vk, user_id, str(exc), kb=guest_keyboard())
        return True

    message = (
        f"Авторизация выполнена.\nОрганизация: {contractor.get('company_name')}\n"
        f"Договор: {contractor.get('contract_number') or 'не указан'}"
    )
    safe_send(vk, user_id, message, kb=partner_keyboard())
    return True


@app.get("/session")
def session_info(vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    return build_session(vk_id)


@app.post("/auth/link")
def auth_link(payload: AuthLinkIn) -> Dict[str, Any]:
    contractor = repo.find_contractor_for_login(payload.identifier)
    if not contractor:
        raise HTTPException(
            404,
            "Контрагент не найден. Проверьте ИНН или номер договора либо оставьте заявку на сотрудничество.",
        )
    if contractor.get("status") == "blocked":
        raise HTTPException(403, "Контрагент заблокирован")
    try:
        contractor = repo.bind_vk_to_contractor(int(contractor["id"]), payload.vk_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"message": "Авторизация выполнена", "session": build_session(payload.vk_id), "contractor": contractor_public_view(contractor)}


@app.post("/auth/logout")
def auth_logout(payload: LogoutIn) -> Dict[str, Any]:
    repo.unbind_vk_from_contractor(payload.vk_id)
    return {"ok": True, "message": "Привязка к организации снята"}


@app.get("/categories")
def categories() -> List[Dict[str, Any]]:
    try:
        return repo.list_categories()
    except Exception:
        logger.exception("categories error")
        raise HTTPException(500, "Не удалось загрузить категории")


@app.get("/products")
def products(category_id: Optional[int] = Query(default=None, ge=1), q: Optional[str] = Query(default=None, max_length=100)) -> List[Dict[str, Any]]:
    try:
        return repo.list_products(category_id=category_id, search=q)
    except Exception:
        logger.exception("products error")
        raise HTTPException(500, "Не удалось загрузить товары")


@app.post("/cart")
def cart_check(payload: CartCheckIn) -> Dict[str, Any]:
    try:
        items, total = repo.validate_cart([item.model_dump() for item in payload.items])
        return {"items": items, "total": total}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        logger.exception("cart validation error")
        raise HTTPException(500, "Не удалось проверить корзину")


@app.post("/lead-requests", status_code=201)
def create_lead_request(payload: LeadRequestIn) -> Dict[str, Any]:
    lead = repo.create_lead_request(
        {
            "vk_id": payload.vk_id,
            "company_name": payload.company_name.strip(),
            "inn": sanitize_digits(payload.inn),
            "contact_name": payload.contact_name.strip(),
            "phone": payload.phone.strip(),
            "city": payload.city.strip(),
            "status": "new",
            "created_at": iso_now(),
        }
    )
    try:
        notify_admins(
            "Новая заявка на сотрудничество.\n\n"
            f"Организация: {lead.get('company_name')}\n"
            f"ИНН: {lead.get('inn')}\n"
            f"Контактное лицо: {lead.get('contact_name')}\n"
            f"Телефон: {lead.get('phone')}\n"
            f"Город: {lead.get('city')}\n\n"
            "Откройте панель управления для рассмотрения."
        )
    except Exception:
        logger.exception("lead admin notification failed lead_id=%s", lead.get("id"))
    return {"message": "Заявка на сотрудничество сохранена", "lead_request": lead}


@app.post("/orders", status_code=201)
def create_order(payload: B2BOrderCreateIn) -> Dict[str, Any]:
    try:
        order = repo.create_b2b_order(
            vk_id=payload.vk_id,
            items=[item.model_dump() for item in payload.items],
            comment=payload.comment,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        logger.exception("create order error")
        raise HTTPException(500, "Не удалось создать заявку")
    return {
        "message": "Заявка создана",
        "order": order_view(order),
        "invoice_pdf_url": order.get("invoice_pdf_url"),
        "payment_hint": (
            "Для запуска в производство необходима предоплата."
            if order.get("payment_type") == "prepayment"
            else "Заявка подтверждена в рамках лимита отсрочки."
        ),
    }


@app.get("/orders")
def partner_orders(vk_id: int = Query(..., ge=1)) -> List[Dict[str, Any]]:
    contractor = ensure_partner(vk_id)
    return [order_view(order) for order in repo.list_orders_for_contractor(int(contractor["id"]))]


@app.get("/orders/{order_id}")
def partner_order(order_id: int, vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_partner(vk_id)
    order = repo.get_order_for_vk_user(vk_id, order_id)
    if not order:
        raise HTTPException(404, "Заявка не найдена")
    order["items"] = repo.list_order_items(order_id)
    return order_view(order)


@app.post("/orders/{order_id}/notify-payment")
def partner_notify_payment(order_id: int, vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_partner(vk_id)
    try:
        order = repo.notify_payment_by_partner(vk_id, order_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"message": "Статус оплаты передан администратору", "order": order_view(order)}


@app.get("/balance")
def balance(vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_partner(vk_id)
    try:
        return repo.build_balance_payload(vk_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@app.get("/documents/list")
def contractor_documents(vk_id: int = Query(..., ge=1)) -> List[Dict[str, Any]]:
    contractor = ensure_partner(vk_id)
    return [contractor_document_view(item) for item in repo.list_contractor_documents(int(contractor["id"]))]


@app.post("/documents/reconciliation")
def reconciliation(vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    contractor = ensure_partner(vk_id)
    orders = repo.list_orders_for_contractor(int(contractor["id"]), limit=20)
    file_name = f"reconciliation_{contractor['id']}_{utcnow().strftime('%Y%m%d%H%M%S')}.pdf"
    file_path = DOCUMENT_DIR / file_name
    file_path.write_bytes(build_reconciliation_pdf(contractor, orders))
    document_url = build_document_file_url(file_name)
    latest_order_id = int(orders[0]["id"]) if orders else None
    try:
        contractor_document = repo.create_contractor_document(
            {
                "contractor_id": contractor["id"],
                "order_id": latest_order_id,
                "document_type": "reconciliation_act",
                "title": f"Акт сверки по договору {contractor.get('contract_number') or ''}".strip(),
                "file_name": file_name,
                "file_url": document_url,
                "status": "active",
                "uploaded_at": iso_now(),
            }
        )
        repo.create_document_request(
            {
                "contractor_id": contractor["id"],
                "order_id": latest_order_id,
                "document_type": "reconciliation_act",
                "status": "done",
                "contractor_document_id": contractor_document.get("id"),
                "comment": "Сформировано автоматически",
                "created_at": iso_now(),
                "updated_at": iso_now(),
            }
        )
        if latest_order_id:
            repo.update_order_fields(latest_order_id, {"reconciliation_requested": True})
    except Exception:
        logger.exception("reconciliation request log failed contractor_id=%s", contractor.get("id"))
    return {
        "message": "Акт сверки сформирован. Документ содержит информацию о заказах, оплатах и текущей задолженности по договору.",
        "company_name": contractor.get("company_name"),
        "document_url": document_url,
    }


@app.post("/documents/duplicate")
def duplicate_document(payload: DuplicateDocumentIn) -> Dict[str, Any]:
    contractor = ensure_partner(payload.vk_id)
    order = repo.get_order_for_vk_user(payload.vk_id, payload.order_id)
    if not order:
        raise HTTPException(404, "Заявка не найдена")
    document_request = repo.create_document_request(
        {
            "contractor_id": contractor["id"],
            "order_id": payload.order_id,
            "document_type": "duplicate_invoice",
            "status": "new",
            "contractor_document_id": None,
            "comment": None,
            "created_at": iso_now(),
            "updated_at": iso_now(),
        }
    )
    try:
        repo.update_order_fields(payload.order_id, {"duplicate_invoice_requested": True})
    except Exception:
        logger.exception("duplicate flag update failed order_id=%s", payload.order_id)
    try:
        notify_admins(
            "Новый запрос документа.\n\n"
            "Тип: Дубликат накладной\n"
            f"Организация: {contractor.get('company_name')}\n"
            f"Договор: {contractor.get('contract_number')}\n"
            f"Заявка: {order.get('order_number') or format_order_number(int(order.get('id') or 0))}\n"
            f"Номер заявки клиента: {format_customer_order_number(order.get('customer_order_number'))}\n\n"
            "Откройте панель управления для обработки запроса."
        )
    except Exception:
        logger.exception("duplicate invoice admin notification failed order_id=%s", payload.order_id)
    return {"message": "Запрос зарегистрирован и передан менеджеру.", "order_id": payload.order_id, "document_request": document_request}


@app.post("/support/request", status_code=201)
def create_support_request(payload: SupportRequestIn) -> Dict[str, Any]:
    support_request = repo.create_support_request(
        {
            "vk_id": payload.vk_id,
            "subject": payload.subject.strip(),
            "message": payload.message.strip(),
            "status": "new",
            "created_at": iso_now(),
        }
    )
    try:
        notify_admins(
            "Новое обращение пользователя.\n\n"
            f"Тема: {support_request.get('subject')}\n"
            f"VK ID: {support_request.get('vk_id')}\n"
            f"Сообщение: {support_request.get('message')}\n\n"
            "Откройте раздел консультаций для обработки."
        )
    except Exception:
        logger.exception("support admin notification failed request_id=%s", support_request.get("id"))
    return {"message": "Обращение зарегистрировано", "support_request": support_request}


@app.get("/documents/invoices/{filename}")
def invoice_file(filename: str):
    safe_name = Path(filename).name
    file_path = INVOICE_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(404, "Файл счёта не найден")
    return FileResponse(file_path, media_type="application/pdf", filename=safe_name)


@app.get("/documents/files/{filename}")
def document_file(filename: str):
    safe_name = Path(filename).name
    file_path = DOCUMENT_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(404, "Файл документа не найден")
    return FileResponse(file_path, media_type="application/pdf", filename=safe_name)


@app.get("/admin/check")
def admin_check(vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    return {"vk_id": vk_id, "is_admin": is_vk_admin(vk_id)}


@app.get("/admin/stats")
def admin_stats(vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_admin(vk_id)
    return repo.get_order_stats()


@app.get("/admin/orders")
def admin_orders(
    vk_id: int = Query(..., ge=1),
    limit: int = Query(default=100, ge=1, le=300),
    status: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    ensure_admin(vk_id)
    if status and status not in ORDER_STATUSES:
        raise HTTPException(400, "Некорректный статус заявки")
    return [order_view(order) for order in repo.list_all_orders(limit=limit, status=status)]


@app.patch("/admin/orders/{order_id}/status")
def admin_update_order_status(order_id: int, payload: AdminOrderStatusPatchIn, vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_admin(vk_id)
    order = repo.get_order(order_id)
    if not order:
        raise HTTPException(404, "Заявка не найдена")

    old_status = str(order.get("status") or "")
    payment_type = str(order.get("payment_type") or "prepayment")
    total_amount = money(order.get("total_amount"))
    contractor_id = int(order.get("contractor_id") or 0)

    update_payload: Dict[str, Any] = {"status": payload.status}
    now_iso = iso_now()
    if payload.status in {"paid", "confirmed"} and not order.get("payment_confirmed_at"):
        update_payload["payment_confirmed_at"] = now_iso
    if payload.status == "shipped" and not order.get("shipped_at"):
        update_payload["shipped_at"] = now_iso
    if payload.status == "completed" and not order.get("completed_at"):
        update_payload["completed_at"] = now_iso
    updated = repo.update_order_fields(order_id, update_payload)

    if payment_type == "deferred":
        old_paid = old_status in PAID_ORDER_STATUSES
        new_paid = payload.status in PAID_ORDER_STATUSES
        if not old_paid and new_paid:
            repo.change_contractor_debt(contractor_id, -total_amount)
        elif old_paid and not new_paid:
            repo.change_contractor_debt(contractor_id, total_amount)
    return {"message": "Статус обновлён", "order": order_view(updated)}


@app.get("/admin/contractors")
def admin_contractors(
    vk_id: int = Query(..., ge=1),
    limit: int = Query(default=100, ge=1, le=300),
    status: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    ensure_admin(vk_id)
    if status and status not in CONTRACTOR_STATUSES:
        raise HTTPException(400, "Некорректный статус контрагента")
    return [contractor_public_view(item) for item in repo.list_contractors(limit=limit, status=status)]


@app.patch("/admin/contractors/{contractor_id}")
def admin_update_contractor(contractor_id: int, payload: ContractorUpdateIn, vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_admin(vk_id)
    update_payload = {key: value for key, value in payload.model_dump().items() if value is not None}
    if "inn" in update_payload:
        update_payload["inn"] = sanitize_digits(str(update_payload["inn"]))
    if "contract_number" in update_payload:
        update_payload["contract_number"] = normalize_identifier(str(update_payload["contract_number"]))
    if not update_payload:
        raise HTTPException(400, "Нет полей для обновления")
    try:
        contractor = repo.update_contractor(contractor_id, update_payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return {"message": "Контрагент обновлён", "contractor": contractor_public_view(contractor)}


@app.get("/admin/leads")
def admin_leads(vk_id: int = Query(..., ge=1), limit: int = Query(default=100, ge=1, le=300)) -> List[Dict[str, Any]]:
    ensure_admin(vk_id)
    return repo.list_lead_requests(limit=limit)


@app.get("/admin/support")
def admin_support_requests(
    vk_id: int = Query(..., ge=1),
    limit: int = Query(default=100, ge=1, le=300),
    status: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    ensure_admin(vk_id)
    if status and status not in {"new", "in_progress", "closed"}:
        raise HTTPException(400, "Некорректный статус обращения")
    return repo.list_support_requests(limit=limit, status=status)


@app.get("/admin/documents")
def admin_documents(
    vk_id: int = Query(..., ge=1),
    limit: int = Query(default=200, ge=1, le=500),
) -> List[Dict[str, Any]]:
    ensure_admin(vk_id)
    return [contractor_document_view(item) for item in repo.list_all_contractor_documents(limit=limit)]


"""
@app.post("/admin/documents/upload", status_code=201)
def admin_upload_document(
    payload: AdminDocumentUploadIn,
    vk_id: int = Query(..., ge=1),
) -> Dict[str, Any]:
    ensure_admin(vk_id)
    if document_type not in {"invoice", "waybill", "contract", "reconciliation_act", "other"}:
        raise HTTPException(400, "Некорректный тип документа")
    contractor = repo.get_contractor_by_id(contractor_id)
    if not contractor:
        raise HTTPException(404, "Контрагент не найден")
    if order_id:
        order = repo.get_order(order_id)
        if not order or int(order.get("contractor_id") or 0) != contractor_id:
            raise HTTPException(404, "Заявка не найдена")
    suffix = Path(file.filename or "document.bin").suffix or ".bin"
    safe_file_name = f"contractor_{contractor_id}_{utcnow().strftime('%Y%m%d%H%M%S')}{suffix}"
    target_path = DOCUMENT_DIR / safe_file_name
    content = await file.read()
    if not content:
        raise HTTPException(400, "Файл не загружен")
    target_path.write_bytes(content)
    file_url = build_document_file_url(safe_file_name)
    document = repo.create_contractor_document(
        {
            "contractor_id": contractor_id,
            "order_id": order_id,
            "document_type": document_type,
            "title": title.strip(),
            "file_name": safe_file_name,
            "file_url": file_url,
            "status": "active",
            "uploaded_at": iso_now(),
        }
    )
    if request_id:
        try:
            repo.update_document_request(
                request_id,
                {
                    "status": "done",
                    "contractor_document_id": document.get("id"),
                    "updated_at": iso_now(),
                },
            )
        except Exception:
            logger.exception("document request attach failed request_id=%s", request_id)
    return {"message": "Документ загружен", "document": contractor_document_view(document)}


"""


@app.post("/admin/documents/upload", status_code=201)
def admin_upload_document(
    payload: AdminDocumentUploadIn,
    vk_id: int = Query(..., ge=1),
) -> Dict[str, Any]:
    ensure_admin(vk_id)
    contractor = repo.get_contractor_by_id(payload.contractor_id)
    if not contractor:
        raise HTTPException(404, "Контрагент не найден")
    if payload.order_id:
        order = repo.get_order(payload.order_id)
        if not order or int(order.get("contractor_id") or 0) != payload.contractor_id:
            raise HTTPException(404, "Заявка не найдена")
    suffix = Path(payload.file_name or "document.bin").suffix or ".bin"
    safe_file_name = f"contractor_{payload.contractor_id}_{utcnow().strftime('%Y%m%d%H%M%S')}{suffix}"
    target_path = DOCUMENT_DIR / safe_file_name
    try:
        content = base64.b64decode(payload.file_content_base64, validate=True)
    except Exception:
        raise HTTPException(400, "Файл повреждён или передан в неверном формате")
    if not content:
        raise HTTPException(400, "Файл не загружен")
    target_path.write_bytes(content)
    file_url = build_document_file_url(safe_file_name)
    document = repo.create_contractor_document(
        {
            "contractor_id": payload.contractor_id,
            "order_id": payload.order_id,
            "document_type": payload.document_type,
            "title": payload.title.strip(),
            "file_name": safe_file_name,
            "file_url": file_url,
            "status": "active",
            "uploaded_at": iso_now(),
        }
    )
    if payload.request_id:
        try:
            repo.update_document_request(
                payload.request_id,
                {
                    "status": "done",
                    "contractor_document_id": document.get("id"),
                    "updated_at": iso_now(),
                },
            )
        except Exception:
            logger.exception("document request attach failed request_id=%s", payload.request_id)
    return {"message": "Документ загружен", "document": contractor_document_view(document)}


@app.patch("/admin/support/{request_id}")
def admin_update_support_request(
    request_id: int,
    payload: SupportRequestStatusIn,
    vk_id: int = Query(..., ge=1),
) -> Dict[str, Any]:
    ensure_admin(vk_id)
    try:
        support_request = repo.update_support_request(request_id, {"status": payload.status})
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return {"message": "Статус обращения обновлён", "support_request": support_request}


@app.get("/admin/document-requests")
def admin_document_requests(
    vk_id: int = Query(..., ge=1),
    limit: int = Query(default=100, ge=1, le=300),
    status: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    ensure_admin(vk_id)
    if status and status not in DOCUMENT_REQUEST_STATUSES:
        raise HTTPException(400, "Некорректный статус запроса документа")
    return repo.list_document_requests(limit=limit, status=status)


@app.patch("/admin/document-requests/{request_id}")
def admin_update_document_request(
    request_id: int,
    payload: DocumentRequestStatusIn,
    vk_id: int = Query(..., ge=1),
) -> Dict[str, Any]:
    ensure_admin(vk_id)
    try:
        document_request = repo.update_document_request(request_id, {"status": payload.status, "updated_at": iso_now()})
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return {"message": "Статус запроса документа обновлён", "document_request": document_request}


@app.post("/admin/leads/{lead_id}/approve")
def admin_approve_lead(lead_id: int, vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_admin(vk_id)
    lead = repo.get_lead_request_by_id(lead_id)
    if not lead:
        raise HTTPException(404, "Заявка на сотрудничество не найдена")

    existing_contractor = repo.get_contractor_by_inn(str(lead.get("inn") or ""))
    if str(lead.get("status") or "") == "done":
        response: Dict[str, Any] = {
            "message": "Заявка уже одобрена",
            "lead_request": lead,
        }
        if existing_contractor:
            response["contractor"] = contractor_public_view(existing_contractor)
        return response

    notification_sent = False
    notification_warning = None

    if existing_contractor:
        updates: Dict[str, Any] = {}
        if lead.get("vk_id") and not existing_contractor.get("vk_id"):
            updates["vk_id"] = lead.get("vk_id")
        if lead.get("phone") and not existing_contractor.get("phone"):
            updates["phone"] = lead.get("phone")
        if lead.get("company_name") and not existing_contractor.get("company_name"):
            updates["company_name"] = lead.get("company_name")
        contractor = repo.update_contractor(int(existing_contractor["id"]), updates) if updates else existing_contractor
    else:
        contract_number = repo.generate_contract_number(lead_id)
        contractor_payload = {
            "company_name": lead.get("company_name"),
            "inn": sanitize_digits(str(lead.get("inn") or "")),
            "vk_id": lead.get("vk_id"),
            "phone": lead.get("phone"),
            "contract_number": contract_number,
            "payment_type": "prepayment",
            "credit_limit": 0,
            "current_debt": 0,
            "payment_days": 0,
            "status": "active",
            "created_at": iso_now(),
        }
        contractor = repo.create_contractor(contractor_payload)

    updated_lead = repo.update_lead_request(lead_id, {"status": "done"})

    user_vk_id = int(lead.get("vk_id") or 0)
    if user_vk_id > 0:
        message = (
            "Ваша заявка на сотрудничество одобрена.\n\n"
            f"Организация: {contractor.get('company_name')}\n"
            f"Номер договора: {contractor.get('contract_number')}\n"
            "Условия оплаты: предоплата\n\n"
            "Теперь вы можете войти в личный кабинет по ИНН или номеру договора."
        )
        notification_sent = send_vk_notification(user_vk_id, message)
        if not notification_sent:
            notification_warning = "Контрагент создан, но уведомление пользователю не отправлено"

    response = {
        "message": notification_warning or "Заявка одобрена, контрагент создан",
        "lead_request": updated_lead,
        "contractor": contractor_public_view(contractor),
        "notification_sent": notification_sent,
    }
    if notification_warning:
        response["warning"] = notification_warning
    return response


@app.post("/admin/reminders/run")
def admin_run_reminders(vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_admin(vk_id)
    reminders = repo.apply_payment_reminders()
    return {"message": "Логика напоминаний выполнена", "reminders": reminders}


@app.post("/admin/products", status_code=201)
def admin_create_product(payload: ProductCreateIn, vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_admin(vk_id)
    return repo.create_product(payload.model_dump())


@app.put("/admin/products/{product_id}")
def admin_update_product(product_id: int, payload: ProductUpdateIn, vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_admin(vk_id)
    patch = {key: value for key, value in payload.model_dump().items() if value is not None}
    if not patch:
        raise HTTPException(400, "Нет полей для обновления")
    try:
        return repo.update_product(product_id, patch)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


@app.delete("/admin/products/{product_id}", status_code=204)
def admin_delete_product(product_id: int, vk_id: int = Query(..., ge=1)) -> Response:
    ensure_admin(vk_id)
    repo.delete_product(product_id)
    return Response(status_code=204)


@app.post("/admin/categories", status_code=201)
def admin_create_category(payload: CategoryIn, vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_admin(vk_id)
    return repo.create_category(payload.name)


@app.put("/admin/categories/{category_id}")
def admin_update_category(category_id: int, payload: CategoryIn, vk_id: int = Query(..., ge=1)) -> Dict[str, Any]:
    ensure_admin(vk_id)
    try:
        return repo.update_category(category_id, payload.name)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


@app.delete("/admin/categories/{category_id}", status_code=204)
def admin_delete_category(category_id: int, vk_id: int = Query(..., ge=1)) -> Response:
    ensure_admin(vk_id)
    repo.delete_category(category_id)
    return Response(status_code=204)


@app.get("/")
def root() -> Dict[str, str]:
    return {"status": "ok", "service": "raipo-backend"}


def handle_text_message(vk: Any, user_id: int, text: str) -> None:
    normalized = (text or "").strip()
    lowered = normalized.lower()
    if pending_support_requests.get(user_id):
        pending_support_requests.pop(user_id, None)
        try:
            subject = "Вопрос пользователя"
            if "документ" in lowered:
                subject = "Документы"
            elif "оплат" in lowered:
                subject = "Оплата"
            elif "заказ" in lowered or "заяв" in lowered:
                subject = "Оформление заказа"
            elif "договор" in lowered or "сотруд" in lowered:
                subject = "Сотрудничество"
            repo.create_support_request(
                {
                    "vk_id": user_id,
                    "subject": subject,
                    "message": normalized,
                    "status": "new",
                    "created_at": iso_now(),
                }
            )
            try:
                notify_admins(
                    "Новое обращение пользователя.\n\n"
                    f"Тема: {subject}\n"
                    f"VK ID: {user_id}\n"
                    f"Сообщение: {normalized}\n\n"
                    "Откройте раздел консультаций для обработки."
                )
            except Exception:
                logger.exception("support admin notification failed from bot user_id=%s", user_id)
            safe_send(
                vk,
                user_id,
                "Обращение зарегистрировано. Менеджер сможет обработать его в панели управления.",
                kb=keyboard_for_role(user_id),
            )
        except Exception:
            logger.exception("Support request save failed user_id=%s", user_id)
            safe_send(
                vk,
                user_id,
                "Не удалось сохранить обращение. Попробуйте ещё раз позже.",
                kb=keyboard_for_role(user_id),
            )
        return
    if not normalized or lowered in {"/start", "start", "привет", "меню", "начать"}:
        show_menu(vk, user_id)
        return
    if try_bind_from_message(vk, user_id, normalized):
        return
    if "контакт" in lowered:
        handle_contacts(vk, user_id)
        return
    if "помощ" in lowered or "справк" in lowered or "консульт" in lowered:
        handle_help(vk, user_id)
        return
    if "каталог" in lowered or "ассортимент" in lowered:
        handle_catalog(vk, user_id)
        return
    if "оплат" in lowered or "лимит" in lowered or "отсроч" in lowered:
        handle_payment_help(vk, user_id)
        return
    if "баланс" in lowered or "задолж" in lowered:
        handle_balance(vk, user_id)
        return
    if "истори" in lowered or "заявк" in lowered:
        handle_history(vk, user_id)
        return
    if "документ" in lowered or "счет" in lowered or "счёт" in lowered:
        handle_documents(vk, user_id)
        return
    if "автор" in lowered or "договор" in lowered or "инн" in lowered:
        handle_auth_help(vk, user_id)
        return
    if "сотруд" in lowered or "партнер" in lowered or "партнёр" in lowered:
        handle_cooperation_help(vk, user_id)
        return
    if "менеджер" in lowered or "вопрос" in lowered or "связаться" in lowered:
        handle_support_prompt(vk, user_id)
        return
    if "статист" in lowered:
        handle_admin_stats(vk, user_id)
        return
    if "райпо" in lowered or "компани" in lowered:
        handle_about(vk, user_id)
        return
    show_menu(vk, user_id)

def should_process_event(event: Any) -> bool:
    if getattr(event, "from_chat", False):
        return False
    if getattr(event, "user_id", None) in (None, 0):
        return False
    return True


def parse_payload(event: Any) -> Optional[Dict[str, Any]]:
    candidates = [
        getattr(event, "payload", None),
        getattr(event, "extra_values", {}).get("payload") if getattr(event, "extra_values", None) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
        if isinstance(candidate, str) and candidate.strip():
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return None


def dispatch_command(vk: Any, user_id: int, command: str) -> None:
    handlers: Dict[str, Callable[[Any, int], None]] = {
        "about": handle_about,
        "contacts": handle_contacts,
        "catalog": handle_catalog,
        "auth_help": handle_auth_help,
        "lead": handle_lead,
        "balance": handle_balance,
        "history": handle_history,
        "documents": handle_documents,
        "help": handle_help,
        "payment_help": handle_payment_help,
        "support": handle_support_prompt,
        "support_list": handle_support_list,
        "logout": handle_logout,
        "admin_stats": handle_admin_stats,
        "contractors": handle_contractors,
        "leads": handle_leads,
        "reminders": handle_reminders,
    }
    handler = handlers.get(command)
    if handler:
        handler(vk, user_id)
    else:
        show_menu(vk, user_id)


def run_bot_sync() -> None:
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    longpoll = VkLongPoll(vk_session)
    logger.info("VK LongPoll запущен")
    while True:
        try:
            for event in longpoll.listen():
                if event.type != VkEventType.MESSAGE_NEW or not event.to_me:
                    continue
                if not should_process_event(event):
                    continue
                user_id = int(event.user_id)
                event_payload = parse_payload(event)
                if event_payload and event_payload.get("cmd"):
                    dispatch_command(vk, user_id, str(event_payload["cmd"]))
                else:
                    handle_text_message(vk, user_id, getattr(event, "text", "") or "")
        except KeyboardInterrupt:
            logger.info("VK LongPoll остановлен")
            break
        except (ApiError, RequestException, TimeoutError, socket.timeout, OSError):
            logger.exception("LongPoll network error, reconnecting in 5s")
            time.sleep(5)
        except Exception:
            logger.exception("LongPoll unexpected error, reconnecting in 5s")
            time.sleep(5)

async def main() -> None:
    bot_task = asyncio.create_task(asyncio.to_thread(run_bot_sync))
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
