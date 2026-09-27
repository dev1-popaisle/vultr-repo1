import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from product_crawler.server import create_app
from product_crawler.store import ProductStore


RUN_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class ServerTests(unittest.TestCase):
    def test_page_and_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "output" / RUN_ID
            folder.mkdir(parents=True)
            (folder / "images").mkdir()
            (folder / "images" / "01-shoe.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (folder / "product.json").write_text(
                json.dumps(
                    {
                        "requested_url": "https://example.com/products/shoe",
                        "title": "Trail Shoe",
                        "brand": "Hoka",
                        "run_id": RUN_ID,
                        "barcode": "100301256016",
                        "blocked_note": (
                            "The original URL in urls.txt was not used because that site blocked the crawler. "
                            "This product was read from https://other.example/shoe instead of "
                            "https://example.com/products/shoe."
                        ),
                        "fetched_url": "https://other.example/shoe",
                        "research": {
                            "average_price": "$155",
                            "sources": ["https://example.com/price"],
                        },
                        "videos": [
                            {
                                "url": "https://www.youtube.com/watch?v=shoe123",
                                "title": "How this shoe feels on a run",
                                "category": "how to use",
                            }
                        ],
                        "reviews": [
                            {
                                "nickname": "Ada",
                                "created_at": "2026-03-01T00:00:00+00:00",
                                "rating": 5,
                                "headline": "Solid",
                                "comments": "Ada liked the cushion on the daily trainer a lot",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (folder / "image-manifest.json").write_text(
                json.dumps(
                    [
                        {
                            "filename": "images/01-shoe.png",
                            "source_url": "https://cdn.example.com/shoe.png",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            app = create_app(root / "products.duckdb", root / "output")
            with TestClient(app) as client:
                page = client.get(f"/{RUN_ID}")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Trail Shoe", page.text)
                self.assertIn("HOKA", page.text)
                self.assertIn(">Brand<", page.text)
                self.assertIn("UPC-A", page.text)
                self.assertIn("100301256016", page.text)
                self.assertIn(f"/{RUN_ID}/image", page.text)
                self.assertIn('id="source-url"', page.text)
                self.assertIn("https://example.com/products/shoe", page.text)
                self.assertIn('id="blocked-note"', page.text)
                self.assertIn("blocked the crawler", page.text)
                self.assertIn("https://other.example/shoe", page.text)
                self.assertIn('id="feedback"', page.text)
                self.assertIn("Ada liked the cushion on the daily trainer a lot", page.text)
                self.assertNotIn("<form", page.text)
                self.assertNotIn(">Save<", page.text)
                self.assertNotIn(">Delete<", page.text)
                self.assertIn('id="videos"', page.text)
                self.assertIn("https://www.youtube.com/watch?v=shoe123", page.text)
                self.assertIn("How this shoe feels on a run", page.text)
                self.assertIn("how to use", page.text)
                self.assertIn('id="research"', page.text)
                self.assertIn("Average market price", page.text)
                self.assertIn("$155", page.text)
                self.assertIn("https://example.com/price", page.text)
                self.assertIn(">Sources<", page.text)
                image = client.get(f"/{RUN_ID}/image")
                self.assertEqual(image.status_code, 200)
                self.assertTrue(image.content.startswith(b"\x89PNG"))

                saved = client.put(
                    f"/api/products/{RUN_ID}",
                    json={"name": "Updated Shoe", "barcode": "100301256016"},
                )
                self.assertEqual(saved.status_code, 200)
                self.assertIn("Updated Shoe", client.get(f"/{RUN_ID}").text)

                listed = client.get("/api/products")
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(listed.json()[0]["id"], RUN_ID)
                full = client.get(f"/api/products/{RUN_ID}")
                self.assertEqual(full.json()["payload"]["title"], "Updated Shoe")

                self.assertEqual(client.delete(f"/api/products/{RUN_ID}").status_code, 204)
                self.assertEqual(client.get(f"/{RUN_ID}").status_code, 404)

                created = client.post(
                    "/api/products",
                    json={"output_id": RUN_ID},
                )
                self.assertEqual(created.status_code, 201)
                self.assertEqual(created.json()["name"], "Trail Shoe")
                home = client.get("/")
                self.assertIn(f'href="/{RUN_ID}"', home.text)

            store = ProductStore(root / "products.duckdb")
            self.assertEqual(store.get(RUN_ID)["name"], "Trail Shoe")

    def test_openai_prompt_results_link_to_the_product_page(self) -> None:
        openai_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(root / "products.duckdb", root / "output")
            with TestClient(app) as client:
                app.state.store.create(
                    {
                        "run_id": openai_id,
                        "origin": "openai_prompt",
                        "title": "Prompt Lamp",
                        "search_prompt": "small cream table lamp",
                    }
                )
                home = client.get("/")
                self.assertIn("Product Search by OpenAI API (Only) :", home.text)
                self.assertIn("Prompt Lamp", home.text)
                self.assertIn(f'href="/{openai_id}"', home.text)
                self.assertIn("The built-in Crawler was not used.", home.text)
                self.assertIn("→", home.text)
                self.assertIn("OpenAI API Only", home.text)
                result = home.text[
                    home.text.find('class="openai-result"') : home.text.find(
                        "</li>", home.text.find('class="openai-result"')
                    )
                ]
                self.assertLess(result.find(f'href="/{openai_id}"'), result.find("→"))
                self.assertLess(result.find("→"), result.find("OpenAI API Only"))
                self.assertLess(
                    home.text.find('class="openai-result"'),
                    home.text.find("Product Search by OpenAI API (Only)"),
                )
                page = client.get(f"/{openai_id}")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Prompt Lamp", page.text)
                self.assertIn('id="page-reset"', home.text)
                self.assertLess(home.text.find('id="page-reset"'), home.text.find("<h1>Product Search Results"))
                self.assertIn('name="prompt"', home.text)
                self.assertIn('action="/search"', home.text)
                self.assertIn("Decision Engine by Jev API (Only) :", home.text)
                self.assertIn('id="jev-question"', home.text)
                self.assertIn(">Decide<", home.text)
                self.assertIn(f'class="jev-select" type="checkbox" value="{openai_id}"', home.text)
                self.assertLess(
                    home.text.find("Product Search by OpenAI API (Only)"),
                    home.text.find("Decision Engine by Jev API (Only)"),
                )


if __name__ == "__main__":
    unittest.main()
