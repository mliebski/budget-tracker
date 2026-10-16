import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Table, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

Money = Numeric(12, 2)

EXPENSE, INCOME = "expense", "income"
KINDS = {EXPENSE: "Expense", INCOME: "Income"}

# type -> (label, is_liability)
ASSET_TYPES = {
    "bank": ("Bank account", False),
    "cash": ("Cash", False),
    "investment": ("Investment (RRSP, TFSA, …)", False),
    "property": ("Property", False),
    "vehicle": ("Vehicle", False),
    "other_asset": ("Other asset", False),
    "mortgage": ("Mortgage", True),
    "loan": ("Loan", True),
    "credit_card": ("Credit card", True),
    "other_liability": ("Other liability", True),
}
LIQUID_TYPES = ("bank", "cash")

transaction_tags = Table(
    "transaction_tags", Base.metadata,
    Column("transaction_id", ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)

recurring_tags = Table(
    "recurring_tags", Base.metadata,
    Column("recurring_id", ForeignKey("recurring.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    parent: Mapped["Category | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Category"]] = relationship(back_populates="parent", order_by="Category.name")

    @property
    def top_id(self) -> int:
        return self.parent_id or self.id

    @property
    def full_name(self) -> str:
        return f"{self.parent.name} › {self.name}" if self.parent else self.name


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("recurring_id", "occurrence_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), index=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    # One-off entries (big purchases, recorded planned items) are left out of
    # the historical averages that drive the forecast.
    one_off: Mapped[bool] = mapped_column(Boolean, default=False)
    recurring_id: Mapped[int | None] = mapped_column(ForeignKey("recurring.id", ondelete="SET NULL"))
    occurrence_date: Mapped[dt.date | None] = mapped_column(Date)
    planned_id: Mapped[int | None] = mapped_column(ForeignKey("planned.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    category: Mapped[Category] = relationship()
    tags: Mapped[list[Tag]] = relationship(secondary=transaction_tags, order_by="Tag.name")
    recurring: Mapped["Recurring | None"] = relationship()

    @property
    def kind(self) -> str:
        return self.category.kind


class Recurring(Base):
    __tablename__ = "recurring"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    amount: Mapped[Decimal] = mapped_column(Money)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    frequency: Mapped[str] = mapped_column(String(12))
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    category: Mapped[Category] = relationship()
    tags: Mapped[list[Tag]] = relationship(secondary=recurring_tags, order_by="Tag.name")
    skips: Mapped[list["RecurringSkip"]] = relationship(cascade="all, delete-orphan")


class RecurringSkip(Base):
    """An occurrence that must not be generated (deleted, or during a pause)."""
    __tablename__ = "recurring_skips"
    __table_args__ = (UniqueConstraint("recurring_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recurring_id: Mapped[int] = mapped_column(ForeignKey("recurring.id", ondelete="CASCADE"))
    date: Mapped[dt.date] = mapped_column(Date)


class Budget(Base):
    """Budget for one category in one fiscal year.

    Month budget = monthly + (annual / 12 if annual_month is None
                              else annual in annual_month only).
    """
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("fiscal_year", "category_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fiscal_year: Mapped[int] = mapped_column(Integer, index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    monthly: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    annual: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    annual_month: Mapped[int | None] = mapped_column(Integer)


class Planned(Base):
    """A known future one-off expense or income (new car, roof repair, ...)."""
    __tablename__ = "planned"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    amount: Mapped[Decimal] = mapped_column(Money)
    date: Mapped[dt.date] = mapped_column(Date)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    note: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(10), default="planned")  # planned | done | cancelled

    category: Mapped[Category] = relationship()


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(255), default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    values: Mapped[list["AssetValue"]] = relationship(
        cascade="all, delete-orphan", order_by="AssetValue.date")

    @property
    def is_liability(self) -> bool:
        return ASSET_TYPES[self.type][1]

    @property
    def type_label(self) -> str:
        return ASSET_TYPES[self.type][0]


class AssetValue(Base):
    __tablename__ = "asset_values"
    __table_args__ = (UniqueConstraint("asset_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    date: Mapped[dt.date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2))


DOC_TYPES = {
    "receipt": "Receipt",
    "invoice": "Invoice",
    "warranty": "Warranty card",
    "manual": "Manual",
    "photo": "Photo",
    "other": "Other",
}


class Item(Base):
    """Something you own: warranty end date, serial number and its documents.

    Linked to the entry (or planned item) that paid for it, or standalone for
    things bought before using the app or received as gifts.
    """
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    store: Mapped[str] = mapped_column(String(100), default="")
    serial_number: Mapped[str] = mapped_column(String(100), default="")
    purchase_date: Mapped[dt.date | None] = mapped_column(Date)
    price: Mapped[Decimal | None] = mapped_column(Money)
    warranty_end: Mapped[dt.date | None] = mapped_column(Date, index=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id", ondelete="SET NULL"))
    planned_id: Mapped[int | None] = mapped_column(ForeignKey("planned.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    transaction: Mapped[Transaction | None] = relationship()
    planned: Mapped[Planned | None] = relationship()
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="item", order_by="Attachment.id")

    def days_left(self, today: dt.date) -> int | None:
        return (self.warranty_end - today).days if self.warranty_end else None


class Attachment(Base):
    """An uploaded photo or PDF, filed under an entry, a planned item or an item.

    Unfiled attachments (no owner) are scans that were never confirmed, or whose
    entry was deleted; they are kept and listed on the Receipts page.
    """
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), index=True)
    planned_id: Mapped[int | None] = mapped_column(ForeignKey("planned.id", ondelete="SET NULL"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"), index=True)
    doc_type: Mapped[str] = mapped_column(String(20), default="receipt")
    filename: Mapped[str] = mapped_column(String(255))       # name as uploaded, for display
    stored_name: Mapped[str] = mapped_column(String(100))    # path below the upload folder
    content_type: Mapped[str] = mapped_column(String(50))
    size: Mapped[int] = mapped_column(Integer)
    has_thumb: Mapped[bool] = mapped_column(Boolean, default=False)
    # OCR result: the raw text (for search) and what was read from it
    ocr_status: Mapped[str] = mapped_column(String(10), default="none")  # none | done | failed
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    store: Mapped[str] = mapped_column(String(100), default="")
    amount: Mapped[Decimal | None] = mapped_column(Money)
    doc_date: Mapped[dt.date | None] = mapped_column(Date)
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    transaction: Mapped[Transaction | None] = relationship()
    planned: Mapped[Planned | None] = relationship()
    item: Mapped[Item | None] = relationship(back_populates="attachments")

    @property
    def is_pdf(self) -> bool:
        return self.content_type == "application/pdf"

    @property
    def filed(self) -> bool:
        return bool(self.transaction_id or self.planned_id or self.item_id)

    # What to show in lists: the confirmed entry wins over what OCR read.
    @property
    def title(self) -> str:
        if self.transaction and self.transaction.note:
            return self.transaction.note
        if self.planned:
            return self.planned.name
        if self.item:
            return self.item.name
        return self.store or self.filename

    @property
    def display_date(self) -> dt.date:
        if self.transaction:
            return self.transaction.date
        return self.doc_date or (self.uploaded_at.date() if self.uploaded_at else dt.date.today())

    @property
    def display_amount(self) -> Decimal | None:
        if self.transaction:
            return self.transaction.amount
        if self.planned:
            return self.planned.amount
        return self.amount if self.amount is not None else (self.item.price if self.item else None)

    @property
    def owner_link(self) -> str | None:
        if self.transaction_id:
            return f"/entries/{self.transaction_id}/edit"
        if self.item_id:
            return f"/warranties/{self.item_id}"
        if self.planned_id:
            return f"/planned?edit={self.planned_id}"
        return None


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
