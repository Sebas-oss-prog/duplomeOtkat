from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

import uvicorn
import vk_api
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, field_validator
from requests.exceptions import RequestException
from supabase import Client, create_client
from vk_api.exceptions import ApiError
from vk_api.longpoll import VkEventType, VkLongPoll
from vk_api.utils import get_random_id

# ==================== 1. НАСТРОЙКИ И ЛОГИРОВАНИЕ ====================
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("raipo-unified")

VK_TOKEN = os.getenv("VK_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
PORT = int(os.getenv("PORT", 7860))
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
VK_APP_ID = os.getenv("VK_APP_ID", "").strip() or None
VK_APP_OWNER_ID = os.getenv("VK_APP_OWNER_ID", "").strip() or None
VK_APP_HASH = os.getenv("VK_APP_HASH", "").strip() or None

# Логирование конфигурации
logger.info("=" * 60)
logger.info("📋 VK Mini App Configuration:")
logger.info(f"   VK_APP_ID: {VK_APP_ID or 'NOT SET'}")
logger.info(f"   VK_APP_OWNER_ID: {VK_APP_OWNER_ID or 'NOT SET'}")
logger.info(f"   VK_APP_HASH: {'SET' if VK_APP_HASH else 'NOT SET (optional)'}")
logger.info(f"   MINI_APP_URL: {MINI_APP_URL or 'NOT SET'}")
logger.info("=" * 60)

if not all([VK_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    raise RuntimeError("Не найдены VK_TOKEN, SUPABASE_URL или SUPABASE_KEY в .env")

# ==================== 2. БАЗА ДАННЫХ (Repo) ====================
ORDER_STATUSES = {"pending", "processing", "assembling", "delivering", "completed", "cancelled"}
DELIVERY_TYPES = {"delivery", "pickup"}
PAYMENT_METHODS = {"card", "sbp", "cash"}


class Repo:
    def __init__(self, supabase_url: str, supabase_key: str) -> None:
        # ✅ Исправлено: убран options=dict, который вызывал ошибку
        self._supabase: Client = create_client(supabase_url, supabase_key)

    def _supabase_request(self, func: Callable, *args, max_retries: int = 3, **kwargs):
        """
        Обёртка для запросов к Supabase с повторными попытками при таймаутах.
        """
        last_error = None
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_error = e
                error_msg = str(e).lower()
                # Повторяем только при таймаутах или временных ошибках сети
                if any(x in error_msg for x in ["timeout", "connection", "read operation", "httpcore"]) and attempt < max_retries - 1:
                    wait_time = 1 * (attempt + 1)  # Экспоненциальная задержка: 1s, 2s, 3s
                    logger.warning(f"⚠️ Попытка {attempt + 1}/{max_retries} не удалась, ждём {wait_time}с...")
                    time.sleep(wait_time)
                    continue
                raise
        raise last_error

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _full_name(self, first_name: str | None, last_name: str | None) -> str | None:
        parts = [p.strip() for p in (first_name, last_name) if p and p.strip()]
        return " ".join(parts) if parts else None

    def get_user(self, vk_id: int) -> Optional[Dict[str, Any]]:
        if vk_id <= 0: return None
        try:
            result = self._supabase_request(
                lambda: self._supabase.table("users").select("*").eq("vk_id", vk_id).limit(1).execute()
            )
            rows = result.data or []
            return rows[0] if rows else None
        except Exception:
            logger.exception("Supabase error loading user vk_id=%s", vk_id)
            return None

    def ensure_user(self, vk_id: int, *, first_name: str | None = None, last_name: str | None = None,
                    phone: str | None = None) -> Dict[str, Any]:
        if vk_id <= 0: raise ValueError("Некорректный vk_id")
        name = self._full_name(first_name, last_name)
        existing = self.get_user(vk_id)
        if existing:
            upd = {"last_interaction": self._now_iso()}
            if first_name and not existing.get("first_name"): upd["first_name"] = first_name
            if last_name and not existing.get("last_name"): upd["last_name"] = last_name
            if name and not existing.get("name"): upd["name"] = name
            if phone: upd["phone"] = phone
            try:
                result = self._supabase_request(
                    lambda: self._supabase.table("users").update(upd).eq("id", existing["id"]).execute()
                )
                updated = result.data or []
                return updated[0] if updated else {**existing, **upd}
            except Exception:
                logger.exception("Supabase error updating user vk_id=%s", vk_id)
                return existing
        payload = {"vk_id": vk_id, "name": name, "first_name": first_name, "last_name": last_name, "phone": phone,
                   "last_interaction": self._now_iso()}
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            result = self._supabase_request(
                lambda: self._supabase.table("users").insert(payload).execute()
            )
            if result.data: return result.data[0]
        except Exception:
            logger.exception("Supabase error creating user vk_id=%s", vk_id)
        existing = self.get_user(vk_id)
        if existing: return existing
        raise RuntimeError("Не удалось создать пользователя")

    def _get_user_id_by_vk_id(self, vk_id: int) -> Optional[int]:
        u = self.get_user(vk_id)
        return int(u["id"]) if u and u.get("id") else None

    def list_categories(self) -> List[Dict[str, Any]]:
        try:
            result = self._supabase_request(
                lambda: self._supabase.table("categories").select("id, name").order("id").execute()
            )
            return result.data or []
        except Exception:
            logger.exception("Supabase error loading categories")
            raise RuntimeError("Не удалось загрузить категории")

    def list_products(self, category_id: int | None = None) -> List[Dict[str, Any]]:
        try:
            q = self._supabase.table("products").select(
                "id, name, description, price, created_at, category_id, image, stock_quantity").order("id")
            if category_id is not None: q = q.eq("category_id", category_id)
            result = self._supabase_request(lambda: q.execute())
            return result.data or []
        except Exception:
            logger.exception("Supabase error loading products")
            raise RuntimeError("Не удалось загрузить товары")

    def search_products(self, query_text: str) -> List[Dict[str, Any]]:
        query_text = (query_text or "").strip()
        if not query_text: raise ValueError("Пустой поисковый запрос")
        try:
            result = self._supabase_request(
                lambda: self._supabase.table("products").select(
                    "id, name, description, price, created_at, category_id, image, stock_quantity"
                ).ilike("name", f"%{query_text}%").order("id").execute()
            )
            return result.data or []
        except Exception:
            logger.exception("Supabase error searching products q=%s", query_text)
            raise RuntimeError("Не удалось выполнить поиск товаров")

    def get_product(self, product_id: int) -> Optional[Dict[str, Any]]:
        if product_id <= 0: return None
        try:
            result = self._supabase_request(
                lambda: self._supabase.table("products").select(
                    "id, name, description, price, created_at, category_id, image, stock_quantity"
                ).eq("id", product_id).limit(1).execute()
            )
            rows = result.data or []
            return rows[0] if rows else None
        except Exception:
            logger.exception("Supabase error loading product id=%s", product_id)
            return None

    def validate_cart(self, items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], float]:
        normalized, total = [], 0.0
        for raw in items:
            pid = int(raw.get("product_id") or raw.get("id") or 0)
            qty = int(raw.get("quantity") or 0)
            if pid <= 0: raise ValueError("Некорректный product_id")
            if qty <= 0 or qty > 999: raise ValueError("Некорректное количество товара")
            prod = self.get_product(pid)
            if not prod: raise ValueError(f"Товар #{pid} не найден")
            stock = prod.get("stock_quantity")
            if stock is not None and int(stock) < qty: raise ValueError(
                f"Недостаточно товара «{prod.get('name') or pid}» на складе")
            price = float(prod.get("price") or 0)
            lt = price * qty
            total += lt
            normalized.append({"product_id": pid, "name": str(prod.get("name") or ""), "price": price, "quantity": qty,
                               "line_total": lt})
        if not normalized: raise ValueError("Корзина пуста")
        return normalized, float(total)

    def create_order(self, *, vk_id: int, first_name: str | None, last_name: str | None, phone: str | None,
                     cart_items: List[Dict[str, Any]], delivery_type: str, address: str, comment: str | None,
                     payment_method: str, pay_now: bool) -> Dict[str, Any]:
        user = self.ensure_user(vk_id, first_name=first_name, last_name=last_name, phone=phone)
        if not user.get("id"): raise RuntimeError("Пользователь не найден")
        norm, total = self.validate_cart(cart_items)
        dt, pm = (delivery_type or "delivery").strip(), (payment_method or "card").strip()
        addr, com = (address or "").strip(), (comment or "").strip() or None
        if dt not in DELIVERY_TYPES: raise ValueError("Некорректный способ получения")
        if pm not in PAYMENT_METHODS: raise ValueError("Некорректный способ оплаты")
        if dt == "delivery" and not addr: raise ValueError("Адрес доставки обязателен")
        ps = "paid" if pay_now and pm != "cash" else "unpaid"
        payload = {"user_id": int(user["id"]), "status": "pending", "total_amount": total, "delivery_type": dt,
                   "address": addr, "comment": com, "payment_method": pm, "payment_status": ps}
        try:
            result = self._supabase_request(
                lambda: self._supabase.table("orders").insert(payload).execute()
            )
        except Exception:
            logger.exception("Supabase error creating order vk_id=%s", vk_id)
            raise RuntimeError("Не удалось создать заказ")
        if not result.data: raise RuntimeError("Supabase вернул пустой ответ")
        order = result.data[0]
        oid = int(order["id"])
        items_p = [{"order_id": oid, "product_id": i["product_id"], "quantity": i["quantity"], "price": i["price"]} for
                   i in norm]
        try:
            self._supabase_request(
                lambda: self._supabase.table("order_items").insert(items_p).execute()
            )
        except Exception:
            logger.exception("Supabase error creating order_items order_id=%s", oid)
            raise RuntimeError("Заказ создан, но не удалось сохранить состав")
        if addr:
            try:
                self._supabase_request(
                    lambda: self._supabase.table("users").update({"last_address": addr}).eq("id", user["id"]).execute()
                )
            except Exception:
                logger.exception("Supabase error updating last_address")
        order["items"] = norm
        return order

    def list_orders(self, vk_id: int) -> List[Dict[str, Any]]:
        uid = self._get_user_id_by_vk_id(vk_id)
        if uid is None: return []
        try:
            result = self._supabase_request(
                lambda: self._supabase.table("orders").select(
                    "id, user_id, status, total_amount, delivery_type, address, comment, payment_method, payment_status, created_at, updated_at, delivery_interval"
                ).eq("user_id", uid).order("id", desc=True).execute()
            )
            return result.data or []
        except Exception:
            logger.exception("Supabase error loading orders vk_id=%s", vk_id)
            return []

    def get_order_for_user(self, vk_id: int, order_id: int) -> Optional[Dict[str, Any]]:
        uid = self._get_user_id_by_vk_id(vk_id)
        if uid is None: return None
        try:
            result = self._supabase_request(
                lambda: self._supabase.table("orders").select(
                    "id, user_id, status, total_amount, delivery_type, address, comment, payment_method, payment_status, created_at, updated_at, delivery_interval"
                ).eq("user_id", uid).eq("id", order_id).limit(1).execute()
            )
            rows = result.data or []
            return rows[0] if rows else None
        except Exception:
            logger.exception("Supabase error loading order_id=%s vk_id=%s", order_id, vk_id)
            return None

    def list_order_items(self, order_id: int) -> List[Dict[str, Any]]:
        try:
            result = self._supabase_request(
                lambda: self._supabase.table("order_items").select("product_id, quantity, price").eq("order_id", order_id).execute()
            )
            items = result.data or []
        except Exception:
            logger.exception("Supabase error loading order_items order_id=%s", order_id)
            return []
        res = []
        for i in items:
            p = self.get_product(int(i["product_id"]))
            res.append(
                {"product_id": int(i["product_id"]), "name": (p or {}).get("name") or f"Товар #{i['product_id']}",
                 "quantity": int(i.get("quantity") or 0), "price": float(i.get("price") or 0)})
        return res

    def update_order_status(self, order_id: int, status: str) -> Dict[str, Any]:
        if status not in ORDER_STATUSES: raise ValueError("Некорректный статус заказа")
        try:
            result = self._supabase_request(
                lambda: self._supabase.table("orders").update({"status": status}).eq("id", order_id).execute()
            )
        except Exception:
            logger.exception("Supabase error updating order status order_id=%s", order_id)
            raise RuntimeError("Не удалось обновить статус заказа")
        if not result.data: raise LookupError("Заказ не найден")
        return result.data[0]


repo = Repo(SUPABASE_URL, SUPABASE_KEY)

# ==================== 3. FASTAPI BACKEND ====================
app = FastAPI(title="RAIPO VK Shop Unified", version="1.0.0")

allow_origins = [o.strip() for o in CORS_ALLOW_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins if allow_origins != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CartItemIn(BaseModel):
    product_id: int = Field(..., ge=1)
    quantity: int = Field(..., ge=1, le=999)


class CartCheckIn(BaseModel):
    items: List[CartItemIn] = Field(default_factory=list)


class OrderCreateIn(BaseModel):
    vk_id: int = Field(..., ge=1)
    first_name: Optional[str] = Field(default=None, max_length=100)
    last_name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=30)
    items: List[CartItemIn] = Field(default_factory=list)
    delivery_type: str = "delivery"
    address: str = Field(default="", max_length=500)
    comment: Optional[str] = Field(default=None, max_length=1000)
    payment_method: str = "card"
    pay_now: bool = False

    @field_validator("delivery_type")
    @classmethod
    def v_dt(cls, v: str) -> str:
        if v not in DELIVERY_TYPES: raise ValueError("Некорректный способ получения")
        return v

    @field_validator("payment_method")
    @classmethod
    def v_pm(cls, v: str) -> str:
        if v not in PAYMENT_METHODS: raise ValueError("Некорректный способ оплаты")
        return v


class OrderStatusPatchIn(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def v_st(cls, v: str) -> str:
        if v not in ORDER_STATUSES: raise ValueError("Некорректный статус заказа")
        return v


def _order_resp(o: Dict[str, Any]) -> Dict[str, Any]:
    return {"order_id": int(o["id"]), "id": int(o["id"]), "user_id": o.get("user_id"), "status": o.get("status"),
            "payment_status": o.get("payment_status"), "total_amount": float(o.get("total_amount") or 0),
            "items": o.get("items", [])}


def _notify(vk_id: int, order_id: int, ps: str | None, vk_inst: Any) -> None:
    try:
        vk_inst.messages.send(user_id=vk_id, random_id=get_random_id(),
                              message=f"Заказ #{order_id} принят.\nСтатус: pending\nОплата: {ps or 'unpaid'}\n\nИсторию и статус можно посмотреть в меню бота.")
    except Exception:
        logger.exception("Не удалось отправить уведомление vk_id=%s order_id=%s", vk_id, order_id)


@app.get("/health")
def health() -> Dict[str, Any]: return {"ok": True, "service": "raipo-vk-shop-unified"}


@app.get("/categories")
def categories() -> List[Dict[str, Any]]:
    try:
        return repo.list_categories()
    except Exception:
        logger.exception("categories error")
        raise HTTPException(500, "Не удалось загрузить категории")


@app.get("/products")
def products() -> List[Dict[str, Any]]:
    try:
        return repo.list_products()
    except Exception:
        logger.exception("products error")
        raise HTTPException(500, "Не удалось загрузить товары")


@app.get("/products/search")
def product_search(q: str = Query(..., min_length=1, max_length=100)) -> List[Dict[str, Any]]:
    try:
        return repo.search_products(q)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.get("/products/{category_id}")
def products_by_cat(cid: int) -> List[Dict[str, Any]]:
    if cid <= 0: raise HTTPException(400, "Некорректный id категории")
    try:
        return repo.list_products(category_id=cid)
    except Exception:
        logger.exception("products_by_cat error")
        raise HTTPException(500, "Не удалось загрузить товары категории")


@app.post("/cart")
def cart_check(p: CartCheckIn) -> Dict[str, Any]:
    try:
        items, total = repo.validate_cart([i.model_dump() for i in p.items])
        return {"items": items, "total": total}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("cart check error")
        raise HTTPException(500, "Ошибка проверки корзины")


@app.post("/orders", status_code=201)
def create_order(p: OrderCreateIn) -> Dict[str, Any]:
    try:
        order = repo.create_order(vk_id=p.vk_id, first_name=p.first_name, last_name=p.last_name, phone=p.phone,
                                  cart_items=[i.model_dump() for i in p.items], delivery_type=p.delivery_type,
                                  address=p.address, comment=p.comment, payment_method=p.payment_method,
                                  pay_now=p.pay_now)
        _notify(p.vk_id, int(order["id"]), order.get("payment_status"), vk_api.VkApi(token=VK_TOKEN).get_api())
        return _order_resp(order)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception:
        logger.exception("create order error")
        raise HTTPException(500, "Внутренняя ошибка сервера")


@app.get("/orders/{vk_id}")
def orders(vk_id: int) -> List[Dict[str, Any]]:
    if vk_id <= 0: raise HTTPException(400, "Некорректный vk_id")
    rows = repo.list_orders(vk_id)
    for r in rows: r["items"] = repo.list_order_items(int(r["id"]))
    return rows


@app.patch("/orders/{order_id}/status")
def update_status(oid: int, p: OrderStatusPatchIn) -> Dict[str, Any]:
    if oid <= 0: raise HTTPException(400, "Некорректный id заказа")
    try:
        return repo.update_order_status(oid, p.status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    fp = Path(__file__).parent / "index.html"
    if fp.exists(): return FileResponse(fp)
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)


# ==================== 4. VK БОТ ====================
user_states: Dict[int, str] = {}
STATE_WAIT_ORDER_ID = "wait_order_id"
STATUS_LABELS = {"pending": "принят", "processing": "в обработке", "assembling": "собирается",
                 "delivering": "в доставке", "completed": "завершён", "cancelled": "отменён"}


def payload(cmd: str, **data: Any) -> str: return json.dumps({"cmd": cmd, **data}, ensure_ascii=False)


def txt_btn(label: str, cmd: str, color: str = "secondary") -> Dict[str, Any]: return {
    "action": {"type": "text", "label": label, "payload": payload(cmd)}, "color": color}


def shop_btn() -> Dict[str, Any]:
    # Приоритет 1: VK Mini App (открывается ВНУТРИ VK)
    if VK_APP_ID and VK_APP_OWNER_ID:
        logger.info(f"📱 Using VK Mini App: app_id={VK_APP_ID}, owner_id={VK_APP_OWNER_ID}")
        return {
            "action": {
                "type": "open_app",
                "app_id": VK_APP_ID,
                "owner_id": VK_APP_OWNER_ID,
                "hash": VK_APP_HASH or "",
                "label": "Сделать заказ"
            }
        }

    # Приоритет 2: Внешняя ссылка (открывается в браузере)
    if MINI_APP_URL and MINI_APP_URL.startswith("https"):
        logger.warning(f"⚠️  Falling back to open_link: {MINI_APP_URL}")
        return {
            "action": {
                "type": "open_link",
                "link": MINI_APP_URL,
                "label": "Сделать заказ"
            }
        }

    # Приоритет 3: Текстовая кнопка
    logger.error("❌ No Mini App or URL configured!")
    return txt_btn("Сделать заказ", "order", "positive")


def main_kb() -> str:
    return json.dumps({"one_time": False, "inline": False, "buttons": [[shop_btn()],
                                                                       [txt_btn("Консультация", "consult", "primary"),
                                                                        txt_btn("История заказов", "history",
                                                                                "primary")],
                                                                       [txt_btn("О предприятии", "about"),
                                                                        txt_btn("Статус заказа", "status")],
                                                                       [txt_btn("Личный кабинет", "profile")]]},
                      ensure_ascii=False)


def consult_kb() -> str:
    return json.dumps({"one_time": False, "inline": False,
                       "buttons": [[txt_btn("🥐 Выпечка", "faq_bakery"), txt_btn("🥛 Молоко", "faq_milk")],
                                   [txt_btn("🔥 Акции", "faq_sales"), txt_btn("🚚 Доставка", "faq_delivery")],
                                   [txt_btn(" Главное меню", "menu")]]}, ensure_ascii=False)


def safe_send(vk: Any, uid: int, msg: str, *, kb: Optional[str] = None) -> None:
    if uid <= 0: return
    params = {"user_id": uid, "message": msg, "random_id": get_random_id()}
    if kb: params["keyboard"] = kb
    try:
        vk.messages.send(**params)
    except ApiError as e:
        if getattr(e, "code", None) == 911 and kb:
            params.pop("keyboard", None)
            vk.messages.send(**params)
        logger.exception("VK send failed")


def parse_evt_pay(evt: Any) -> Optional[Dict[str, Any]]:
    raw = getattr(evt, "payload", None)
    if not raw: return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def get_vk_user_info(vk: Any, uid: int) -> Dict[str, Optional[str]]:
    try:
        info = vk.users.get(user_ids=uid)
        if info and isinstance(info, list):
            r = info[0]
            return {"first_name": str(r.get("first_name") or "").strip() or None,
                    "last_name": str(r.get("last_name") or "").strip() or None}
    except Exception:
        logger.exception("VK user fetch failed uid=%s", uid)
    return {"first_name": None, "last_name": None}


def fmt_order(o: Dict[str, Any]) -> str:
    st = STATUS_LABELS.get(str(o.get("status")), str(o.get("status") or "неизвестно"))
    return f"Заказ #{o.get('id')}\nСтатус: {st}\nОплата: {o.get('payment_status') or 'unpaid'}\nСумма: {float(o.get('total_amount') or 0):.2f} ₽\nДата: {str(o.get('created_at') or 'дата не указана')}"


def show_menu(vk: Any, uid: int) -> None:
    safe_send(vk, uid,
              "Здравствуйте! Это сервис заказов РАЙПО г. Слободской.\n\nЗдесь можно оформить доставку, посмотреть историю заказов и узнать статус.",
              kb=main_kb())


def handle_order(vk: Any, uid: int) -> None:
    if VK_APP_ID and VK_APP_OWNER_ID:
        hint = "Нажмите кнопку «Сделать заказ» ниже, чтобы открыть магазин внутри VK."
    else:
        hint = f"Откройте магазин: {MINI_APP_URL or '/'}"
    safe_send(vk, uid, "🛒 Магазин доступен по кнопке ниже.\n\n" + hint, kb=main_kb())


def handle_consult(vk: Any, uid: int) -> None:
    safe_send(vk, uid,
              "Помогу с выбором товаров, доставкой и оформлением заказа.\nМожно написать, например: «хлеб», «молоко», «акции», «график работы».",
              kb=consult_kb())


def handle_about(vk: Any, uid: int) -> None:
    safe_send(vk, uid,
              "РАЙПО г. Слободской — местная торговая организация с товарами первой необходимости, выпечкой, молочной продукцией и продовольственными товарами.\n\nАдреса: г. Слободской, торговые точки РАЙПО.\nГрафик: ежедневно 08:00–20:00.",
              kb=main_kb())


def handle_history(vk: Any, uid: int) -> None:
    orders = repo.list_orders(uid)
    if not orders:
        safe_send(vk, uid, "История пока пустая.", kb=main_kb())
        return
    lines = ["Последние заказы:"]
    for o in orders[:10]:
        lines.append(
            f"#{o.get('id')} — {STATUS_LABELS.get(str(o.get('status')), '?')}, {float(o.get('total_amount') or 0):.2f} ₽, {str(o.get('created_at') or '')[:16]}")
    lines.append("\nДля подробностей нажмите «Статус заказа» и введите номер.")
    safe_send(vk, uid, "\n".join(lines), kb=main_kb())


def handle_status_prompt(vk: Any, uid: int) -> None:
    user_states[uid] = STATE_WAIT_ORDER_ID
    safe_send(vk, uid, "Введите номер заказа, например: 125", kb=main_kb())


def handle_status(vk: Any, uid: int, oid: int) -> None:
    o = repo.get_order_for_user(uid, oid)
    if not o:
        safe_send(vk, uid, f"Заказ #{oid} не найден.", kb=main_kb())
        return
    items = repo.list_order_items(oid)
    lines = [fmt_order(o)]
    if items:
        lines.append("\nСостав:")
        for i in items: lines.append(f"• {i['name']} × {i['quantity']} = {float(i['price']) * i['quantity']:.2f} ₽")
    safe_send(vk, uid, "\n".join(lines), kb=main_kb())


def handle_profile(vk: Any, uid: int) -> None:
    u = repo.get_user(uid) or {}
    ords = repo.list_orders(uid)
    fn = u.get("first_name") or get_vk_user_info(vk, uid).get("first_name") or "не указано"
    la = next((o.get("address") for o in ords if o.get("address")), "адресов пока нет")
    safe_send(vk, uid, f"Личный кабинет\n\nИмя: {fn}\nVK ID: {uid}\nЗаказов: {len(ords)}\nПоследний адрес: {la}",
              kb=main_kb())


def faq(txt: str) -> str | None:
    n = txt.lower()
    if any(w in n for w in ("хлеб", "выпеч",
                            "булоч")): return "По выпечке доступны хлеб, батоны, булочки. Точный ассортимент смотрите в каталоге."
    if any(w in n for w in ("молоко", "кефир",
                            "творог")): return "Молочная продукция в соответствующей категории. Откройте каталог для проверки наличия."
    if "акци" in n or "скид" in n: return "Акции отображаются в каталоге. Напишите интересующий товар."
    if "график" in n or "работ" in n: return "График работы: ежедневно с 08:00 до 20:00."
    if "где" in n or "адрес" in n: return "Магазины расположены в г. Слободской. Адрес уточните у оператора."
    if "достав" in n: return "Доставка оформляется в каталоге. Выберите товары и укажите адрес."
    if "заказ" in n or "оформ" in n: return "Откройте каталог, добавьте товары в корзину и подтвердите оформление."
    return None


def dispatch(vk: Any, uid: int, cmd: str) -> None:
    h = {"menu": show_menu, "order": handle_order, "consult": handle_consult, "history": handle_history,
         "about": handle_about, "status": handle_status_prompt, "profile": handle_profile,
         "faq_bakery": lambda v, u: safe_send(v, u, faq("выпечка") or "", kb=consult_kb()),
         "faq_milk": lambda v, u: safe_send(v, u, faq("молоко") or "", kb=consult_kb()),
         "faq_sales": lambda v, u: safe_send(v, u, faq("акции") or "", kb=consult_kb()),
         "faq_delivery": lambda v, u: safe_send(v, u, faq("доставка") or "", kb=consult_kb())}
    (h.get(cmd) or show_menu)(vk, uid)


def handle_text(vk: Any, uid: int, text: str) -> None:
    text = (text or "").strip()
    if not text: show_menu(vk, uid); return
    if user_states.get(uid) == STATE_WAIT_ORDER_ID:
        user_states.pop(uid, None)
        m = re.search(r"\d+", text)
        if m:
            handle_status(vk, uid, int(m.group()))
        else:
            safe_send(vk, uid, "Не вижу номер заказа. Нажмите «Статус заказа» и введите цифры.", kb=main_kb())
        return
    n = text.lower()
    if n in ("/start", "start", "начать", "привет", "меню"): show_menu(vk, uid); return
    if "сделать заказ" in n or "магазин" in n: handle_order(vk, uid); return
    if "консультац" in n or "помощ" in n: handle_consult(vk, uid); return
    if "истори" in n: handle_history(vk, uid); return
    if "статус" in n:
        m = re.search(r"\d+", text)
        handle_status(vk, uid, int(m.group())) if m else handle_status_prompt(vk, uid)
        return
    if "предприят" in n or "райпо" in n: handle_about(vk, uid); return
    if "кабинет" in n or "профиль" in n: handle_profile(vk, uid); return
    ans = faq(text)
    if ans: safe_send(vk, uid, ans + "\n\nОткройте каталог для оформления заказа.", kb=consult_kb()); return
    show_menu(vk, uid)


def run_bot_sync() -> None:
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    longpoll = VkLongPoll(vk_session)
    logger.info("✅ VK LongPoll запущен")
    while True:
        try:
            for evt in longpoll.listen():
                if evt.type != VkEventType.MESSAGE_NEW or not evt.to_me: continue
                uid = int(evt.user_id)
                u_info = get_vk_user_info(vk, uid)
                try:
                    repo.ensure_user(uid, first_name=u_info["first_name"], last_name=u_info["last_name"])
                except Exception:
                    logger.exception("User register failed uid=%s", uid)
                ep = parse_evt_pay(evt)
                if ep and ep.get("cmd"):
                    dispatch(vk, uid, str(ep["cmd"]))
                else:
                    handle_text(vk, uid, getattr(evt, "text", "") or "")
        except KeyboardInterrupt:
            logger.info("🛑 VK LongPoll stopped")
            break
        except (ApiError, RequestException, TimeoutError, socket.timeout, OSError):
            logger.exception("🔄 LongPoll network error, reconnecting in 5s")
            time.sleep(5)
        except Exception:
            logger.exception("🔄 LongPoll unexpected error, reconnecting in 5s")
            time.sleep(5)


# ==================== 5. ЗАПУСК ====================
async def main():
    # Запускаем бота в фоне (синхронный longpoll в отдельном потоке)
    bot_task = asyncio.create_task(asyncio.to_thread(run_bot_sync))

    # Запускаем FastAPI сервер
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