import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Boolean, ForeignKey, Text, text,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker, relationship

DB_DIR = os.environ.get("DB_DIR", "data")
DB_PATH = os.path.join(DB_DIR, "homecharts.db")
os.makedirs(DB_DIR, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class Listing(Base):
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(String, unique=True, nullable=False, index=True)
    portal = Column(String, nullable=False)
    title = Column(String)
    url = Column(String)
    city = Column(String, default="Batumi")
    country = Column(String, default="Georgia")
    neighborhood = Column(String)
    price = Column(Float)
    currency = Column(String, default="USD")
    area_sqm = Column(Float)
    rooms = Column(Integer)
    floor = Column(Integer)
    total_floors = Column(Integer)
    image_url = Column(String)
    images = Column(Text)          # JSON array of image URLs
    phone = Column(String)         # Contact phone number (for WhatsApp link)
    description = Column(Text)
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    last_scraped_at = Column(DateTime)
    is_active = Column(Boolean, default=True)

    price_history = relationship(
        "PriceHistory",
        back_populates="listing",
        order_by="PriceHistory.recorded_at",
        cascade="all, delete-orphan",
    )

    @property
    def price_drop_pct(self) -> float:
        """Percentage change from peak price to current (negative = drop)."""
        if not self.price_history or self.price is None:
            return 0.0
        peak = max(ph.price for ph in self.price_history)
        if peak == 0:
            return 0.0
        return ((self.price - peak) / peak) * 100

    @property
    def max_price(self) -> float:
        if not self.price_history:
            return self.price or 0.0
        return max(ph.price for ph in self.price_history)

    @property
    def initial_price(self) -> float:
        if not self.price_history:
            return self.price or 0.0
        return self.price_history[0].price


class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    listing_id = Column(
        Integer, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    price = Column(Float, nullable=False)
    currency = Column(String, default="USD")
    recorded_at = Column(DateTime, default=datetime.utcnow)
    change_pct = Column(Float, default=0.0)

    listing = relationship("Listing", back_populates="price_history")


class EmailAlert(Base):
    __tablename__ = "email_alerts"

    id             = Column(Integer, primary_key=True, index=True)
    listing_id     = Column(Integer, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False)
    email          = Column(String, nullable=False, index=True)
    created_at     = Column(DateTime, default=datetime.utcnow)
    last_notified_at    = Column(DateTime, nullable=True)
    last_notified_price = Column(Float, nullable=True)
    is_active      = Column(Boolean, default=True)

    listing = relationship("Listing")


def create_tables() -> None:
    Base.metadata.create_all(bind=engine)
    # Non-destructive migration: add new columns to existing DBs
    with engine.connect() as conn:
        for col_def in ("images TEXT", "phone TEXT"):
            try:
                conn.execute(text(f"ALTER TABLE listings ADD COLUMN {col_def}"))
                conn.commit()
            except Exception:
                pass  # column already exists


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
