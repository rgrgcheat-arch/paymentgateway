from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, DateTime, Text, ForeignKey, UniqueConstraint, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column
from db import Base
def now(): return datetime.now(timezone.utc).replace(tzinfo=None)
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    username: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(190), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(20))
    pw_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="merchant")  # owner|seller|reseller|merchant
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|suspended
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    business_name: Mapped[str] = mapped_column(String(120), default="")
    business_type: Mapped[str] = mapped_column(String(40), default="")
    gstin: Mapped[str] = mapped_column(String(20), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    photo_b64: Mapped[str] = mapped_column(Text, default="")
    photo_mime: Mapped[str] = mapped_column(String(20), default="")
    photo_v: Mapped[int] = mapped_column(Integer, default=0)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    notif_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    terms_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
class GatewayCred(Base):
    __tablename__ = "gateway_credentials"
    __table_args__ = (UniqueConstraint("user_id", "gateway"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    gateway: Mapped[str] = mapped_column(String(20))
    data_enc: Mapped[str] = mapped_column(Text)
    env: Mapped[str] = mapped_column(String(12), default="staging")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_msg: Mapped[str] = mapped_column(String(300), default="")
class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String(60), default="")
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_enc: Mapped[str] = mapped_column(Text, default="")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    last_used: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
class Txn(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("user_id", "merchant_order_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    pay_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    merchant_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gateway: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(Integer)  # paise
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    customer: Mapped[str] = mapped_column(Text, default="{}")
    qr_data: Mapped[str] = mapped_column(Text, default="")
    provider_ref: Mapped[str] = mapped_column(String(80), default="")
    msg_key: Mapped[str] = mapped_column(String(64), default="")  # id of the bank/FamPay email that confirmed this payment (one email can confirm only one order)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    updated: Mapped[datetime] = mapped_column(DateTime, default=now)
class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(String(300), default="")
    created: Mapped[datetime] = mapped_column(DateTime, default=now)

class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    price: Mapped[int] = mapped_column(Integer, default=0)  # paise
    duration_days: Mapped[int] = mapped_column(Integer, default=30)  # 0 = never expires
    req_limit: Mapped[int] = mapped_column(Integer, default=1000)
    txn_limit: Mapped[int] = mapped_column(Integer, default=100)
    max_keys: Mapped[int] = mapped_column(Integer, default=1)
    gateways: Mapped[str] = mapped_column(String(100), default="paytm")
    support: Mapped[str] = mapped_column(String(40), default="Community")
    code: Mapped[str] = mapped_column(String(20), default="")  # free|trial|fampay|paytm
    prices: Mapped[str] = mapped_column(Text, default="")  # JSON {"30": paise, "90": paise, ...}
    hours: Mapped[int] = mapped_column(Integer, default=0)  # trial length in hours
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    start: Mapped[datetime] = mapped_column(DateTime, default=now)
    end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    price_paid: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="active")  # active|replaced|expired
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[int] = mapped_column(Integer, default=0)  # list value of the whole period (basis for unused-day credit); 0 = use price_paid
    days: Mapped[int] = mapped_column(Integer, default=0)  # total days of the period (0 = derive from start/end)
    source: Mapped[str] = mapped_column(String(12), default="admin")  # admin|payment|trial|credit
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    url: Mapped[str] = mapped_column(String(500))
    secret_enc: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_retries: Mapped[int] = mapped_column(Integer, default=5)
class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True)
    event: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str] = mapped_column(String(220), default="")
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[int] = mapped_column(primary_key=True)
    txn_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    refund_id: Mapped[str] = mapped_column(String(40), unique=True)
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="PENDING")
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
class UserSettings(Base):
    __tablename__ = "user_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(30), default="")
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(String(300))
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
class LoginLog(Base):
    __tablename__ = "login_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    identity: Mapped[str] = mapped_column(String(120))
    ip: Mapped[str] = mapped_column(String(60))
    ok: Mapped[bool] = mapped_column(Boolean)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)

class Setting(Base):
    __tablename__ = "site_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")

class SubOrder(Base):  # a plan purchase; paid through the OWNER's own gateway, auto-verified
    __tablename__ = "sub_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    days: Mapped[int] = mapped_column(Integer, default=30)
    list_price: Mapped[int] = mapped_column(Integer, default=0)  # paise
    credit: Mapped[int] = mapped_column(Integer, default=0)  # paise, unused-day credit applied
    amount: Mapped[int] = mapped_column(Integer, default=0)  # paise actually payable
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)  # PENDING|PAID|EXPIRED|FAILED|CANCELLED
    pay_id: Mapped[str] = mapped_column(String(40), default="")
    sub_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
class Broadcast(Base):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(primary_key=True)
    message: Mapped[str] = mapped_column(String(300))
    audience: Mapped[str] = mapped_column(String(40), default="all")
    count: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
class MusicTrack(Base):  # owner-uploaded background music; audio bytes live in the database so they survive redeploys
    __tablename__ = "music_tracks"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    mime: Mapped[str] = mapped_column(String(60), default="audio/mpeg")
    size: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
