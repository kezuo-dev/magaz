"""Модель данных. Ядро — книга (единичный экземпляр) и её лоты на площадках."""
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Marketplace(str, Enum):
    OZON = "ozon"
    WB = "wildberries"


class BookStatus(str, Enum):
    DRAFT = "draft"          # черновик, ещё не выставлена
    IN_STOCK = "in_stock"    # в наличии, выставлена хотя бы где-то
    SOLD = "sold"            # продана — снимается со всех площадок
    WITHDRAWN = "withdrawn"  # снята с продажи вручную


class ListingStatus(str, Enum):
    PENDING = "pending"        # запланировано, ещё не отправлено на площадку
    ACTIVE = "active"          # опубликовано и активно
    WITHDRAWING = "withdrawing"  # снятие отправлено, ждём подтверждения
    WITHDRAWN = "withdrawn"    # снято
    ERROR = "error"            # ошибка синхронизации, см. sync_log


class Book(Base):
    """Экземпляр книги. Остаток на площадках = quantity (обычно 1)."""

    __tablename__ = "books"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512), index=True)
    author: Mapped[str | None] = mapped_column(String(255), index=True, default=None)
    isbn: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    publisher: Mapped[str | None] = mapped_column(String(255), default=None)
    year: Mapped[int | None] = mapped_column(Integer, default=None)
    condition: Mapped[str | None] = mapped_column(String(64), default=None)  # состояние б/у
    price: Mapped[float | None] = mapped_column(Numeric(10, 2), default=None)
    # Количество экземпляров — остаток, который уходит на склад площадки («Мои
    # склады»). Для книги б/у обычно 1, но можно указать больше одинаковых.
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    photos: Mapped[str | None] = mapped_column(Text, default=None)  # ссылки на фото через перевод строки
    status: Mapped[str] = mapped_column(String(16), default=BookStatus.DRAFT, index=True)

    # Категория/тип товара на площадках — обязательны при создании карточки.
    # Хранятся у книги (а не в ключах площадки), чтобы разные книги можно было
    # относить к разным разделам каталога.
    ozon_category_id: Mapped[str | None] = mapped_column(String(32), default=None)  # description_category_id
    ozon_type_id: Mapped[str | None] = mapped_column(String(32), default=None)      # type_id
    wb_subject_id: Mapped[str | None] = mapped_column(String(32), default=None)     # subjectID «Книги»

    # Жанр («Направление») — обязательный атрибут книжной категории Ozon и у каждой
    # книги свой. Храним id значения из справочника Ozon и его подпись для показа.
    ozon_direction_id: Mapped[str | None] = mapped_column(String(32), default=None)
    ozon_direction_name: Mapped[str | None] = mapped_column(String(128), default=None)

    # Габариты и вес — тоже обязательны для карточки Ozon. Пусто = берём дефолты из
    # настроек (типичная книга). Габариты в мм, вес в граммах, вместе с упаковкой.
    weight_grams: Mapped[int | None] = mapped_column(Integer, default=None)
    length_mm: Mapped[int | None] = mapped_column(Integer, default=None)
    width_mm: Mapped[int | None] = mapped_column(Integer, default=None)
    height_mm: Mapped[int | None] = mapped_column(Integer, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    listings: Mapped[list["Listing"]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )

    @property
    def photo_list(self) -> list[str]:
        return [p.strip() for p in (self.photos or "").splitlines() if p.strip()]


class Listing(Base):
    """Привязка книги к конкретной площадке и состояние публикации там."""

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("book_id", "marketplace", name="uq_book_marketplace"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), index=True)
    marketplace: Mapped[str] = mapped_column(String(16), index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True, default=None)  # ID лота на площадке
    # Ключ, по которому площадка отдаёт остаток FBS. Для Ozon это offer_id (= наш
    # SKU), для WB — баркод (skus[0]), который может отличаться от vendorCode.
    # Храним отдельно, чтобы слежение за остатками спрашивало площадку по её ключу.
    stock_key: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    status: Mapped[str] = mapped_column(String(16), default=ListingStatus.PENDING, index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    book: Mapped["Book"] = relationship(back_populates="listings")


class Order(Base):
    """Входящий заказ с площадки. Триггер авто-снятия книги с остальных."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("marketplace", "external_order_id", name="uq_marketplace_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace: Mapped[str] = mapped_column(String(16), index=True)
    external_order_id: Mapped[str] = mapped_column(String(128), index=True)
    book_id: Mapped[int | None] = mapped_column(ForeignKey("books.id"), index=True, default=None)
    external_sku: Mapped[str | None] = mapped_column(String(128), default=None)  # что прислала площадка
    processed: Mapped[bool] = mapped_column(default=False, index=True)  # обработан ли (снята ли книга)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncLog(Base):
    """Журнал всех действий с площадками. Критично для разбора ошибок на объёме 50k."""

    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace: Mapped[str | None] = mapped_column(String(16), index=True, default=None)
    book_id: Mapped[int | None] = mapped_column(ForeignKey("books.id"), index=True, default=None)
    action: Mapped[str] = mapped_column(String(64))   # publish / withdraw / poll_orders / import ...
    ok: Mapped[bool] = mapped_column(default=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class MarketplaceAccount(Base):
    """Ключи/токены доступа к площадкам. Секреты хранятся шифрованно."""

    __tablename__ = "marketplace_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    credentials_encrypted: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
