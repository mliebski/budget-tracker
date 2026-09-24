"""Starter categories, created once when the database is empty."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import EXPENSE, INCOME, Category

DEFAULT_CATEGORIES = {
    EXPENSE: {
        "Housing": ["Rent / Mortgage", "Property tax", "Home insurance", "Maintenance & repairs"],
        "Utilities": ["Electricity", "Heating / Gas", "Water", "Internet", "Phone"],
        "Transportation": ["Fuel", "Car insurance", "Car maintenance", "Transit", "Parking",
                           "Car payment"],
        "Food": ["Groceries", "Restaurants", "Coffee & snacks"],
        "Health": ["Pharmacy", "Dental", "Vision", "Fitness"],
        "Personal": ["Clothing", "Personal care", "Gifts", "Education"],
        "Entertainment": ["Subscriptions", "Hobbies", "Travel", "Events"],
        "Financial": ["Bank fees", "Loan payments", "Charity"],
        "Kids & Pets": ["Childcare", "Pet care"],
        "Other": [],
    },
    INCOME: {
        "Salary": [],
        "Bonus": [],
        "Government benefits": ["CCB", "GST/HST credit"],
        "Investment income": [],
        "Other income": [],
    },
}


def seed_categories(db: Session) -> None:
    if db.scalar(select(Category.id).limit(1)):
        return
    for kind, groups in DEFAULT_CATEGORIES.items():
        for top_name, subs in groups.items():
            top = Category(name=top_name, kind=kind)
            db.add(top)
            for sub in subs:
                db.add(Category(name=sub, kind=kind, parent=top))
    db.commit()
