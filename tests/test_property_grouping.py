import unittest

from dashboard.server import (
    _group_property_items,
    _has_street_and_house,
    _location_precision,
    _same_property,
)


def property_item(
    listing_pk: int,
    source: str,
    *,
    street: str,
    seller_phone: str,
    price: int,
) -> dict:
    return {
        "listing_pk": listing_pk,
        "platform_listing_id": str(listing_pk),
        "source": source,
        "url": f"https://{source}.example/{listing_pk}",
        "title": "2-комнатная квартира",
        "price": price,
        "current_price": price,
        "previous_price": None,
        "previous_currency": None,
        "currency": "USD",
        "price_changed_at": None,
        "published_at": f"2026-09-{listing_pk:02d} 12:00:00",
        "first_seen_at": "2026-09-01 12:00:00",
        "last_seen_at": "2026-09-10 12:00:00",
        "last_checked_at": "2026-09-10 12:00:00",
        "last_changed_at": None,
        "description_length": 100,
        "status": "active",
        "transaction_type": "rent",
        "scope": "city",
        "city": "Ташкент",
        "district": "Мирабадский район",
        "street": street,
        "rooms": 2,
        "area_m2": 50,
        "floor": 3,
        "floors_total": 9,
        "latitude": 41.3001,
        "longitude": 69.2812,
        "seller_name": f"Seller {listing_pk}",
        "seller_phone": seller_phone,
        "seller_group": "professional",
        "amenities": [],
        "nearby_places": [],
    }


class PropertyGroupingTest(unittest.TestCase):
    def test_same_housing_from_different_sources_and_sellers_is_one_item(self) -> None:
        first = property_item(
            1, "olx", street="ул. Нукусская, 12", seller_phone="998901111111", price=700
        )
        second = property_item(
            2, "realt24", street="Нукусская 12", seller_phone="998902222222", price=650
        )

        self.assertTrue(_same_property(first, second))
        grouped = _group_property_items([first, second])

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["publication_count"], 2)
        self.assertEqual(grouped[0]["sources"], ["olx", "realt24"])
        self.assertEqual([offer["price"] for offer in grouped[0]["offers"]], [700, 650])

    def test_different_housing_is_not_merged_only_because_seller_matches(self) -> None:
        first = property_item(
            1, "olx", street="Нукусская 12", seller_phone="998901111111", price=700
        )
        second = property_item(
            2, "realting", street="Шота Руставели 80", seller_phone="998901111111", price=700
        )
        second["rooms"] = 4
        second["area_m2"] = 110

        self.assertFalse(_same_property(first, second))
        self.assertEqual(len(_group_property_items([first, second])), 2)

    def test_same_housing_repeated_on_one_platform_is_marked_as_duplicate(self) -> None:
        first = property_item(
            3, "olx", street="Нукусская 12", seller_phone="998901111111", price=700
        )
        second = property_item(
            4, "olx", street="ул. Нукусская, 12", seller_phone="998902222222", price=680
        )

        grouped = _group_property_items([first, second])

        self.assertEqual(len(grouped), 1)
        self.assertTrue(grouped[0]["is_duplicate"])
        self.assertEqual(grouped[0]["duplicate_scope"], "same_platform")

    def test_group_preserves_coordinates_from_non_representative_publication(self) -> None:
        first = property_item(
            5, "olx", street="ул. Нукусская, 12", seller_phone="998901111111", price=700
        )
        second = property_item(
            6, "realt24", street="ул. Нукусская, 12", seller_phone="998901111111", price=650
        )
        first["latitude"] = None
        first["longitude"] = None
        first["description_length"] = 500

        grouped = _group_property_items([first, second])

        self.assertEqual(grouped[0]["latitude"], second["latitude"])
        self.assertEqual(grouped[0]["longitude"], second["longitude"])
        self.assertEqual(grouped[0]["location_precision"], "coordinates")

    def test_location_precision_keeps_only_district_only_items_off_map(self) -> None:
        self.assertTrue(_has_street_and_house("улица Шота Руставели, 93"))
        self.assertTrue(_has_street_and_house("ул. Нукусская 12Б"))
        self.assertFalse(_has_street_and_house("улица Шота Руставели"))
        self.assertFalse(_has_street_and_house("6-й квартал"))
        self.assertEqual(
            _location_precision({"street": "улица Шота Руставели, 93"}),
            "address",
        )
        self.assertEqual(
            _location_precision({"street": "улица Шота Руставели"}),
            "address",
        )
        self.assertEqual(
            _location_precision({"residential_complex_name": "ЖК Gardens"}),
            "address",
        )
        self.assertEqual(_location_precision({"district": "Мирабадский район"}), "district")


if __name__ == "__main__":
    unittest.main()
