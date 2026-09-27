# Product page crawler

Reads a file of product URLs and, for each URL that does not already have an output folder, writes a folder of still images plus a JSON record of the product fields on that page.

It does not search, follow related products, or crawl the rest of the site. The only extra requests are same-product resources needed to complete the record:

- Shopify `product.js` when the URL path is `/products/{handle}`
- the PowerReviews request named by the widget on that same page, when reviews are not in the HTML

Video is not downloaded. Still images are limited to jpg, png, webp, and avif gallery and variant images.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
python3 -m product_crawler urls.txt
```

After install, the `product-crawler` command is the same entry point.

The command takes one URL file. If you omit it, or the file is missing, the tool prints an error and exits with status `2` without fetching anything.

`urls.txt` is one absolute `http` or `https` URL per line. A line that already ends with a UUID folder name is skipped. After a successful crawl, that UUID is written on the same line:

```text
https://www.sportsbasement.com/products/clifton-16?variant=41172026130504 23d0c747-1512-4212-9bf3-b61636fc83e7
https://example.com/products/next-shoe
```

A `.json` file is the same list as objects. Pending URLs use `"folder": null`. Finished ones store the UUID folder name:

```json
[
  {
    "url": "https://www.sportsbasement.com/products/clifton-16?variant=41172026130504",
    "folder": "23d0c747-1512-4212-9bf3-b61636fc83e7"
  }
]
```

The `variant` query parameter selects which variant is recorded. Prices from Shopify `product.js` are converted from cents. A failed URL stays without a folder name so a later run can try it again.

## Output

Each run creates `output/<uuid>/`:

```text
output/<uuid>/
  product.json           product fields from the page and same-product endpoints
  image-manifest.json    local image filenames mapped to source URLs
  images/                gallery and variant stills
```

`product.json` includes the title, brand, selected variant, price, currency, availability, SKU, description, dimensions, specs, materials, weight, ranking, popularity, star rating, review count, breadcrumbs, and options when those fields are present. `reviews` holds the 10 latest user feedbacks from whatever section that page uses: a review feed named on the page, structured review data, or visible review, feedback, or comment blocks. Each comment is stored at up to 100 words. When none are found, `reviews` is an empty list and `reviews_note` is `No user feedback found on this page.` Missing fields are JSON `null`.

A sample run for the Hoka Men's Clifton 11 page (`/products/clifton-16`, variant `41172026130504`) is committed at `output/23d0c747-1512-4212-9bf3-b61636fc83e7/`.

## Pages

After a crawl, the product JSON is stored as one DuckDB row. The local server reads that table.

```bash
python3 -m product_crawler.server
```

Open http://127.0.0.1:8888/<uuid>. The page shows one product image, the product name, the brand in uppercase, and the market barcode (UPC-A, EAN-13, GTIN-14, or EAN-8 when the stored code matches that length). The sample page is http://127.0.0.1:8888/23d0c747-1512-4212-9bf3-b61636fc83e7 and its brand is HOKA.

The same UUID is the API record:

- `GET /api/products` lists rows
- `GET /api/products/<uuid>` returns the row, including the full scraped document
- `POST /api/products` stores a new document or imports `output/<uuid>`
- `PUT /api/products/<uuid>` updates the name, brand, and barcode
- `DELETE /api/products/<uuid>` removes the row

The database file is `data/products.duckdb`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
