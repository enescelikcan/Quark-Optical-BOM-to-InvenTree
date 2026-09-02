# Altium BOM -> InvenTree Importer

Takes one or more Altium BOM exports (`.xlsx`) and, for each one, creates
(or updates) a matching "project" Assembly Part in InvenTree, with a BOM
built entirely from parts that already exist in InvenTree.

**This tool never creates new components.** It only looks up existing
parts by IPN and links them into a BOM. The only new Part it ever
creates is the Assembly Part representing the project itself.

## Setup

```bash
pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json` with your InvenTree server address and credentials:

```json
{
  "server": "https://your-inventree-server",
  "username": "your_username",
  "password": "your_password"
}
```

In InvenTree, make sure a category named **"Projects"** already exists
(this tool looks it up but does not create it).

## Run

```bash
python main.py
```

Then click **Select BOM file(s)...**, pick one or more `.xlsx` exports,
and click **Start**.

## How matching works

- The BOM's **Manufacturer Part Number** column is matched against each
  InvenTree part's **IPN** field.
- The **project name** is the BOM file's name (without the `.xlsx`
  extension) -- it is not read from inside the file.
- The **Quantity** column becomes the BomItem quantity.
- The **Value** column (if present) and any unnamed columns (e.g. LCSC
  codes, free-text notes) are ignored.
- Columns are matched by header name, not position, so BOM exports with
  slightly different column layouts are both handled correctly.

## What happens with unmatched parts

If some BOM lines have no matching IPN in InvenTree, you'll see a table
listing them before anything is written. You can either continue
(those lines are left out of the BOM) or cancel that file and go add
the missing parts to InvenTree first.

## What happens if a project already exists

If an Assembly Part with the same name already exists in the "Projects"
category, you'll be asked whether to:

- **Update** its existing BOM (the current BOM is cleared and rebuilt
  from the file), or
- **Create a new version** (a new Assembly Part named e.g.
  `ProjectName (v2)` is created alongside the existing one).

## Known limitations / possible next steps

- IPN and category name are assumed to be unique in your InvenTree
  instance -- if they aren't, the tool raises an error rather than
  guessing.
- Processing runs on the GUI thread. For very large BOMs or many files
  at once, this could be moved into a background `QThread`.
