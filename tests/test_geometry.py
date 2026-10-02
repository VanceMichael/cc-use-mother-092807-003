"""几何：面积、重叠、相接不算重叠。"""

import unittest

from src.restoration.geometry import polygon_area_m2, polygons_overlap


def box(lon, lat, dlon=0.001, dlat=0.001):
    return {
        "type": "Polygon",
        "coordinates": [
            [[lon, lat], [lon + dlon, lat], [lon + dlon, lat + dlat],
             [lon, lat + dlat], [lon, lat]]
        ],
    }


class GeometryTest(unittest.TestCase):
    def test_area_positive_for_valid_polygon(self) -> None:
        self.assertGreater(polygon_area_m2(box(0, 0)), 10_000)

    def test_overlapping_polygons_detected(self) -> None:
        self.assertTrue(polygons_overlap(box(0, 0), box(0.0005, 0.0005)))

    def test_contained_polygon_detected(self) -> None:
        self.assertTrue(polygons_overlap(box(0, 0), box(0.0002, 0.0002, 0.0003, 0.0003)))

    def test_disjoint_polygons_do_not_overlap(self) -> None:
        self.assertFalse(polygons_overlap(box(0, 0), box(0.1, 0.1)))

    def test_edge_contact_is_not_overlap(self) -> None:
        self.assertFalse(polygons_overlap(box(0, 0), box(0.001, 0)))

    def test_multipolygon_supported(self) -> None:
        geom = {"type": "MultiPolygon", "coordinates": [box(0, 0)["coordinates"]]}
        self.assertTrue(polygons_overlap(geom, box(0.0005, 0.0005)))


if __name__ == "__main__":
    unittest.main()
