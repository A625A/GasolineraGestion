"""Interactive command-line console for the gasolinera toolkit."""

from __future__ import annotations

import os
import textwrap
from datetime import datetime, timedelta

from gasolinera.analytics import PeakHourAnalyzer, sample_sales_log
from gasolinera.contacts import Contact, ContactBook, sample_contact_book
from gasolinera.data import sample_usage_dataframe
from gasolinera.forecast import MonthlyUsageForecaster
from gasolinera.inventory import InventoryManager
from gasolinera.mapping import LocationRecommender, build_folium_map, sample_geo_dataframe
from gasolinera.notifications import ConsoleNotifier, NotificationMessage, NotificationService
from gasolinera.scheduling import DeliveryScheduler, GoogleCalendarClient, suggest_delivery_window
from gasolinera.security import AuthService


MENU_WIDTH = 72
DIVIDER = "=" * MENU_WIDTH


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def print_banner(title: str) -> None:
    print(DIVIDER)
    print(f"🌐 {title}".center(MENU_WIDTH))
    print(DIVIDER)


def render_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("No records to display.\n")
        return

    col_widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            col_widths[index] = max(col_widths[index], len(cell))

    def fmt_row(items: list[str]) -> str:
        return "│ " + " │ ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(items)) + " │"

    border_top = "┌" + "┬".join("─" * (w + 2) for w in col_widths) + "┐"
    border_mid = "├" + "┼".join("─" * (w + 2) for w in col_widths) + "┤"
    border_bottom = "└" + "┴".join("─" * (w + 2) for w in col_widths) + "┘"

    print(border_top)
    print(fmt_row(headers))
    print(border_mid)
    for row in rows:
        print(fmt_row(row))
    print(border_bottom)
    print()


def prompt(message: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{message}{suffix}: ").strip()
    return value or (default or "")


def menu_forecast() -> None:
    clear_screen()
    print_banner("Monthly Usage Forecast")
    usage_df = sample_usage_dataframe()
    fuel_types = sorted(usage_df["gasoline_type"].unique())
    print("Available gasoline types:")
    for index, fuel in enumerate(fuel_types, start=1):
        print(f"  {index}. {fuel}")
    choice = prompt("Select fuel type", default="1")
    try:
        fuel_type = fuel_types[int(choice) - 1]
    except (ValueError, IndexError):
        fuel_type = fuel_types[0]

    periods = prompt("Months to predict", default="6")
    try:
        periods_int = max(1, int(periods))
    except ValueError:
        periods_int = 6

    forecaster = MonthlyUsageForecaster(model="prophet")
    forecaster.fit(usage_df, gasoline_type=fuel_type)
    forecast = forecaster.predict(periods=periods_int)
    print(f"\nProjected demand for {fuel_type} (next {periods_int} months):")
    render_table(["Month", "Forecast (L)", "Lower", "Upper"], [
        [
            row["ds"].strftime("%Y-%m"),
            f"{row['yhat']:.0f}",
            f"{row['yhat_lower']:.0f}",
            f"{row['yhat_upper']:.0f}",
        ]
        for _, row in forecast.iterrows()
    ])
    input("Press Enter to return to the main menu...")


def menu_peak_analysis() -> None:
    clear_screen()
    print_banner("Peak Hour Insights")
    sales_df = sample_sales_log()
    analyzer = PeakHourAnalyzer()
    analyzer.fit(sales_df)
    summary = analyzer.get_station_summary("CDMX-01")
    if not summary:
        print("No peak data available.\n")
    else:
        print("Top service hours:")
        rows = [[f"{int(hour):02d}:00", f"{volume:.0f} L"] for hour, volume in summary.top_hours.head(5).items()]
        render_table(["Hour", "Litres"], rows)
        print("Demand by weekday:")
        rows = [[weekday, f"{volume:.0f} L"] for weekday, volume in summary.by_weekday.items()]
        render_table(["Weekday", "Litres"], rows)
    input("Press Enter to return to the main menu...")


def menu_scheduling_notifications() -> None:
    clear_screen()
    print_banner("Scheduling & Notifications")
    calendar = GoogleCalendarClient()
    scheduler = DeliveryScheduler(calendar)
    notifier = NotificationService([ConsoleNotifier()])

    start_time = datetime.utcnow() + timedelta(days=1)
    event = scheduler.schedule_fuel_delivery(
        "Regular Restock",
        start=start_time,
        station_id="CDMX-01",
        notes="Coordinate with supplier ACME Fuel.",
    )
    eta = suggest_delivery_window(last_delivery=start_time, usage_rate_per_day=4500, reorder_threshold=12000)
    notifier.send(
        NotificationMessage(
            subject="Delivery scheduled",
            body=f"{event.summary} on {event.start:%Y-%m-%d %H:%M}. Suggested next reorder around {eta:%Y-%m-%d %H:%M}.",
            category="delivery",
        )
    )

    print(textwrap.dedent(
        f"""
        • Delivery scheduled for {event.metadata.get('station_id')} on {event.start:%Y-%m-%d %H:%M}.
        • Reminder sent to console (see above).
        • Estimated reorder threshold at {eta:%Y-%m-%d %H:%M}.
        """
    ).strip())
    input("Press Enter to return to the main menu...")


def menu_mapping() -> None:
    clear_screen()
    print_banner("Location Recommendations")
    geo_df = sample_geo_dataframe()
    recommender = LocationRecommender(n_clusters=3)
    try:
        recommender.fit(geo_df)
        suggestions = recommender.recommend()
    except RuntimeError as exc:
        print(f"Unable to generate recommendations: {exc}\n")
        input("Press Enter to continue...")
        return

    print("Suggested growth clusters:")
    rows = [
        [
            f"{row.latitude:.4f}",
            f"{row.longitude:.4f}",
            f"{row.potential_demand:.0f}",
        ]
        for row in suggestions.itertuples(index=False)
    ]
    render_table(["Latitude", "Longitude", "Demand Score"], rows)

    fmap = build_folium_map(geo_df, suggestions)
    if fmap is not None:
        output = "location_recommendations.html"
        fmap.save(output)
        print(f"Map saved to {output}. Open it in a browser to explore markers.\n")
    input("Press Enter to return to the main menu...")


def menu_inventory_security() -> None:
    clear_screen()
    print_banner("Inventory & Security")
    inventory = InventoryManager()
    inventory.record_delivery("CDMX-01", "Regular", volume=30000, capacity=50000)
    inventory.consume("CDMX-01", "Regular", volume=12000)

    records = []
    for key, record in inventory.get_snapshot().items():
        records.append([record.station_id, record.gasoline_type, f"{record.current_level:.0f} L", f"{record.capacity:.0f} L"])
    render_table(["Station", "Fuel", "Current Level", "Capacity"], records)

    auth = AuthService()
    user = auth.register_user("admin", "example-password", roles={"admin", "dispatcher"})
    print(f"Authentication success: {bool(auth.authenticate('admin', 'example-password'))}")
    print(f"Roles for {user.username}: {', '.join(sorted(user.roles))}\n")
    input("Press Enter to return to the main menu...")


def menu_contacts(contact_book: ContactBook) -> None:
    while True:
        clear_screen()
        print_banner("Contact Directory")
        print("Categories:", ", ".join(contact_book.categories) or "(none)")
        print(
            textwrap.dedent(
                """
                1. List all contacts
                2. List contacts by category
                3. Add new contact
                4. Add category
                5. Search contacts
                0. Back to main menu
                """
            ).strip()
        )
        choice = prompt("Choose an option", default="1")
        if choice == "1":
            contacts = contact_book.list_contacts()
            render_contacts(contacts)
            input("Press Enter to continue...")
        elif choice == "2":
            category = prompt("Enter category name")
            contacts = contact_book.list_contacts(category=category)
            render_contacts(contacts, title=f"Contacts in '{category}'")
            input("Press Enter to continue...")
        elif choice == "3":
            add_contact_flow(contact_book)
        elif choice == "4":
            category = prompt("New category name")
            if category:
                contact_book.add_category(category)
        elif choice == "5":
            query = prompt("Search for")
            contacts = contact_book.search(query)
            render_contacts(contacts, title=f"Search results for '{query}'")
            input("Press Enter to continue...")
        elif choice == "0":
            break
        else:
            continue


def render_contacts(contacts: list[Contact], *, title: str | None = None) -> None:
    clear_screen()
    print_banner(title or "Contacts")
    rows = []
    for contact in contacts:
        rows.append(
            [
                contact.name,
                contact.company or "-",
                contact.phone or "-",
                contact.email or "-",
                ", ".join(contact.categories) or "-",
            ]
        )
    render_table(["Name", "Company", "Phone", "Email", "Categories"], rows)


def add_contact_flow(contact_book: ContactBook) -> None:
    clear_screen()
    print_banner("Add Contact")
    name = prompt("Name")
    if not name:
        print("Contact not created (name required).")
        input("Press Enter to continue...")
        return
    company = prompt("Company", default="")
    phone = prompt("Phone", default="")
    email = prompt("Email", default="")
    notes = prompt("Notes", default="")
    categories_raw = prompt("Categories (comma separated)", default="")
    categories = [c.strip() for c in categories_raw.split(",") if c.strip()]

    contact = contact_book.create_contact(
        name=name,
        company=company or None,
        phone=phone or None,
        email=email or None,
        notes=notes or None,
        categories=categories,
    )
    print(f"\nContact {contact.name} added with ID {contact.contact_id}.")
    input("Press Enter to continue...")


def menu_api_reference() -> None:
    clear_screen()
    print_banner("API Quick Reference")
    endpoints = [
        ("GET /api/health", "API heartbeat"),
        ("GET /api/logos", "List available fuel logos"),
        ("POST /api/usage/upload", "Upload usage CSV"),
        ("GET /api/forecast/<type>", "Predict consumption per fuel"),
        ("GET /api/analytics/peaks", "Retrieve peak hour summary"),
        ("GET /api/mapping/recommendations", "Suggested station clusters"),
        ("GET /api/inventory", "Current tank levels"),
        ("POST /api/inventory/delivery", "Record deliveries"),
        ("GET /api/schedule/deliveries", "List upcoming deliveries"),
        ("POST /api/schedule/deliveries", "Schedule a delivery"),
        ("GET /api/contacts", "List contacts"),
        ("POST /api/contacts", "Create contact entry"),
    ]
    render_table(["Endpoint", "Description"], [[path, desc] for path, desc in endpoints])
    print("Launch the API with: python -m gasolinera.gasolinera.api\n")
    input("Press Enter to return to the main menu...")


def main() -> None:
    contact_book = sample_contact_book()
    while True:
        clear_screen()
        print_banner("Gasolinera Operations Console")
        print(
            textwrap.dedent(
                """
                1. Monthly usage forecast
                2. Peak hour insights
                3. Scheduling & notifications
                4. Location recommendations
                5. Inventory & security snapshot
                6. Contact directory
                7. API quick reference
                0. Exit
                """
            ).strip()
        )
        choice = prompt("Choose an option", default="1")
        if choice == "1":
            menu_forecast()
        elif choice == "2":
            menu_peak_analysis()
        elif choice == "3":
            menu_scheduling_notifications()
        elif choice == "4":
            menu_mapping()
        elif choice == "5":
            menu_inventory_security()
        elif choice == "6":
            menu_contacts(contact_book)
        elif choice == "7":
            menu_api_reference()
        elif choice == "0":
            clear_screen()
            print("Goodbye! 👋")
            break


if __name__ == "__main__":
    main()
