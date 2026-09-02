"""
inventree_client.py

Thin wrapper around the official `inventree` Python package
(https://pypi.org/project/inventree/), exposing only the operations this
tool needs:

  - look up an existing Part by its IPN field (matched against the BOM's
    "Manufacturer Part Number" column)
  - find/create the single "project" Assembly Part inside a given
    category
  - replace or version an Assembly Part's BOM

Important: this tool NEVER creates new components. It only looks up
parts that already exist in InvenTree and links them into a BOM. The
only new Part it ever creates is the Assembly Part representing the
project itself.
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from inventree.api import InvenTreeAPI
from inventree.part import Part, PartCategory, BomItem


@dataclass
class InventreeConfig:
    server: str
    username: str
    password: str

    @classmethod
    def load(cls, path: str) -> "InventreeConfig":
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(
                f"'{path}' not found. Create a config.json file in this "
                f"folder with the following content:\n"
                f"{{\n"
                f'    "server": "https://your-inventree-server",\n'
                f'    "username": "your_username",\n'
                f'    "password": "your_password"\n'
                f"}}"
            )
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        try:
            return cls(
                server=data["server"],
                username=data["username"],
                password=data["password"],
            )
        except KeyError as missing_key:
            raise ValueError(
                f"config.json is missing required field: {missing_key}"
            )


class InventreeClient:
    """Wraps an authenticated InvenTree API session and the specific
    operations this tool performs against it."""

    def __init__(self, config: InventreeConfig, timeout: int = 30):
        # The inventree-python library's own default timeout (10s) turned
        # out to be too short once several requests are in flight at the
        # same time (see add_bom_items_concurrently) -- a busy/shared
        # server (e.g. the public InvenTree demo instance) can take
        # longer than that to answer each one under concurrent load.
        self.api = InvenTreeAPI(
            config.server,
            username=config.username,
            password=config.password,
            timeout=timeout,
        )

    # ---------------------------------------------------------------
    # Category
    # ---------------------------------------------------------------

    def get_category_by_name(self, name: str) -> PartCategory:
        """Find an existing category by exact name.

        Does NOT create the category -- it is expected to already exist
        (the user creates it manually once, e.g. "Projects")."""
        matches = [c for c in PartCategory.list(self.api) if c.name == name]
        if not matches:
            raise LookupError(
                f"Category '{name}' was not found in InvenTree. "
                f"Please create it manually first."
            )
        if len(matches) > 1:
            raise LookupError(
                f"Multiple categories named '{name}' exist in InvenTree. "
                f"Category names must be unique for this tool to work "
                f"reliably -- please rename or merge them."
            )
        return matches[0]

    # ---------------------------------------------------------------
    # Part lookup by IPN (this is how BOM lines are matched)
    # ---------------------------------------------------------------
    #
    # Looking up parts one IPN at a time (one network request per BOM
    # line) is the main reason imports were slow -- an 82-line BOM meant
    # 82 separate round trips just for matching, before even writing
    # anything. Instead, we fetch every part ONCE (build_ipn_index) and
    # do all the matching locally in memory (resolve_part_by_ipn), which
    # turns "one request per BOM line" into "one request per run".

    def build_ipn_index(self) -> dict:
        """Fetch every Part in InvenTree ONCE, and group them by IPN.

        Returns a dict: IPN -> list of Parts sharing that IPN (almost
        always a list of length 1 -- longer lists mean InvenTree has
        more than one part registered under the same IPN, which
        resolve_part_by_ipn() will flag as an error when that IPN is
        actually looked up).
        """
        index: dict = {}
        for part in Part.list(self.api):
            ipn = getattr(part, "IPN", None)
            if not ipn:
                continue
            index.setdefault(ipn, []).append(part)
        return index

    def resolve_part_by_ipn(self, ipn: str, ipn_index: dict) -> Optional[Part]:
        """Look up a single Part by IPN inside an index built by
        build_ipn_index() -- no network call, this is a local dict
        lookup.

        Returns None if no part has this IPN.

        Raises LookupError if more than one Part shares the IPN --
        InvenTree does not enforce IPN uniqueness by default, so this
        can genuinely happen and must not be silently guessed at.
        """
        matches = ipn_index.get(ipn, [])
        if not matches:
            return None
        if len(matches) > 1:
            raise LookupError(
                f"IPN '{ipn}' matches {len(matches)} different parts in "
                f"InvenTree. IPN must be unique for this tool to work "
                f"reliably."
            )
        return matches[0]

    # ---------------------------------------------------------------
    # Assembly (project) part
    # ---------------------------------------------------------------

    def find_assembly_by_name(
        self, name: str, category: PartCategory
    ) -> Optional[Part]:
        """Look up an existing Assembly Part by exact name, within the
        given category only (so a project can't collide with an
        unrelated component that happens to share a name).

        IMPORTANT: we deliberately do NOT pass name=... as a server-side
        filter. InvenTree's API silently drops filter parameters it
        doesn't recognise instead of raising an error -- so a filter
        that isn't wired up server-side just returns everything, and
        the previous version of this method (relying on that filter)
        was picking up whatever part happened to be first in the
        category, regardless of its name. Fetching by category and
        filtering by exact name in Python avoids depending on that
        server-side behaviour entirely.
        """
        candidates = Part.list(self.api, category=category.pk)
        matches = [p for p in candidates if p.name == name]
        if not matches:
            return None
        if len(matches) > 1:
            raise LookupError(
                f"Multiple parts named '{name}' exist in category "
                f"'{category.name}'. Part names must be unique within "
                f"the category for this tool to work reliably."
            )
        return matches[0]

    def create_assembly(self, name: str, category: PartCategory) -> Part:
        return Part.create(
            self.api,
            {
                "name": name,
                "description": "",
                "category": category.pk,
                "assembly": True,
                "active": True,
            },
        )

    def next_version_name(self, base_name: str, category: PartCategory) -> str:
        """Generate a name like 'base_name (v2)' that does not already
        exist in the given category, for the 'create a new version'
        workflow."""
        version = 2
        while True:
            candidate = f"{base_name} (v{version})"
            if self.find_assembly_by_name(candidate, category) is None:
                return candidate
            version += 1

    # ---------------------------------------------------------------
    # BOM
    # ---------------------------------------------------------------

    def clear_bom(self, assembly: Part) -> None:
        """Delete every existing BomItem under this assembly, so it can
        be rebuilt from scratch (used by the 'update existing project'
        workflow)."""
        for item in BomItem.list(self.api, part=assembly.pk):
            item.delete()

    def add_bom_item(self, assembly: Part, sub_part: Part, quantity: float) -> None:
        BomItem.create(
            self.api,
            {
                "part": assembly.pk,
                "sub_part": sub_part.pk,
                "quantity": quantity,
            },
        )

    def add_bom_items_concurrently(
        self, assembly: Part, items: list, max_workers: int = 5, retries: int = 1
    ) -> list:
        """Create several BomItems at once.

        InvenTree's BOM API has no bulk-create endpoint (confirmed by
        reading the underlying inventree-python library's create()
        method -- it always sends and expects exactly one object per
        request), so this cannot be turned into a single request the
        way build_ipn_index() was for reads. Instead, it sends several
        individual create requests IN PARALLEL using a thread pool: each
        request still waits on the network the same as before, but up
        to `max_workers` of them are now in flight at once instead of
        queued one after another.

        A shared/public server can occasionally take longer to answer
        a request than usual once several are in flight at once (we
        saw this happen against the InvenTree demo server -- a handful
        of requests timed out under load, even though nothing was
        actually wrong with them). So after the first pass, any item
        that failed is retried sequentially (one at a time, not in
        parallel, so the retry doesn't recreate the same contention)
        up to `retries` more times before being reported as a real
        failure.

        `items` is a list of (BomLine, Part) tuples, as produced by
        match_lines() in main.py -- BomLine only needs a `.quantity`
        attribute here, so this method doesn't need to import that
        class.

        Returns a list of (item, Exception) for any item that still
        failed after all retries, so the caller can report exactly
        which ones didn't make it in.
        """
        failures = self._add_bom_items_batch(assembly, items, max_workers)

        attempt = 0
        while failures and attempt < retries:
            attempt += 1
            retry_items = [item for item, _exc in failures]
            failures = self._add_bom_items_batch(assembly, retry_items, max_workers=1)

        return failures

    def _add_bom_items_batch(self, assembly: Part, items: list, max_workers: int) -> list:
        """One pass over `items`, creating up to `max_workers` BomItems
        in parallel. Returns (item, Exception) for anything that failed
        in this pass -- used by add_bom_items_concurrently() both for
        the initial parallel attempt and for each sequential retry."""
        failures = []

        def _create(item):
            line, part = item
            self.add_bom_item(assembly, part, line.quantity)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {executor.submit(_create, item): item for item in items}
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    future.result()
                except Exception as exc:
                    failures.append((item, exc))

        return failures
