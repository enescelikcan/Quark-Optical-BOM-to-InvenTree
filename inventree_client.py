"""
inventree_client.py

Thin wrapper around the official `inventree` Python package
(https://pypi.org/project/inventree/), exposing only the operations this
tool needs:

  - look up an existing Part by its name field (matched against the
    BOM's "Comment" column)
  - find/create the single "project" Assembly Part inside a given
    category
  - replace or version an Assembly Part's BOM
  - read back an existing Assembly Part's BOM, to compare it against a
    BOM file without changing anything (see CheckWorker in main.py)

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
    # Part lookup by name (this is how BOM lines are matched)
    # ---------------------------------------------------------------
    #
    # Looking up parts one name at a time (one network request per BOM
    # line) is the main reason imports were slow -- an 82-line BOM meant
    # 82 separate round trips just for matching, before even writing
    # anything. Instead, we fetch every part ONCE (build_name_index) and
    # do all the matching locally in memory (resolve_part_by_name), which
    # turns "one request per BOM line" into "one request per run".
    #
    # This used to match on the IPN field instead (against the BOM's
    # "Manufacturer Part Number" column). That was switched to name
    # (against the BOM's "Comment" column) because some IPN values
    # turned out to be blank or duplicated across otherwise-unrelated
    # parts, whereas name is unique for every part except
    # placeholder/not-yet-permanently-named ones -- which we still want
    # this tool to flag as ambiguous rather than silently guess at (see
    # resolve_part_by_name() below).

    def build_name_index(self) -> dict:
        """Fetch every Part in InvenTree ONCE, and group them by name.

        Returns a dict: name -> list of Parts sharing that name (almost
        always a list of length 1 -- longer lists mean InvenTree has
        more than one part registered under the same name, typically
        placeholder parts that haven't been given a permanent, unique
        name yet. resolve_part_by_name() will flag this as an error
        when that name is actually looked up).
        """
        index: dict = {}
        for part in Part.list(self.api):
            name = getattr(part, "name", None)
            if not name:
                continue
            index.setdefault(name, []).append(part)
        return index

    def resolve_part_by_name(self, name: str, name_index: dict) -> Optional[Part]:
        """Look up a single Part by name inside an index built by
        build_name_index() -- no network call, this is a local dict
        lookup.

        Returns None if no part has this name.

        Raises LookupError if more than one Part shares the name --
        this happens for placeholder parts that haven't been given a
        permanent, unique name yet, and must not be silently guessed
        at (the part needs a unique name in InvenTree before it can be
        matched reliably).
        """
        matches = name_index.get(name, [])
        if not matches:
            return None
        if len(matches) > 1:
            raise LookupError(
                f"Name '{name}' matches {len(matches)} different parts "
                f"in InvenTree (likely a placeholder part that hasn't "
                f"been given a permanent, unique name yet). Give it a "
                f"unique name in InvenTree before it can be matched "
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
    #
    # Both clearing an existing BOM (many deletes) and populating a new
    # one (many creates) mean firing off one InvenTree API request per
    # BomItem -- there's no bulk endpoint for either. The two private
    # helpers below (_run_concurrently, _run_with_retries) implement
    # "do this for many items, in parallel, with retries" ONCE, and
    # clear_bom() / add_bom_items_concurrently() just plug in which
    # operation to run.

    def _run_concurrently(self, items: list, operation, max_workers: int) -> list:
        """Run operation(item) for each item in `items`, using up to
        max_workers threads at once. Returns a list of (item, Exception)
        for any item whose operation raised."""
        failures = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {executor.submit(operation, item): item for item in items}
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    future.result()
                except Exception as exc:
                    failures.append((item, exc))
        return failures

    def _run_with_retries(
        self, items: list, operation, max_workers: int, retries: int,
        filter_pending=None,
    ) -> list:
        """Like _run_concurrently(), but retries failed items
        sequentially (max_workers=1) up to `retries` more times before
        giving up on them.

        A shared/public server can occasionally take longer to answer a
        request than usual once several are in flight at once (we saw
        this happen against the InvenTree demo server -- a handful of
        requests timed out under load, even though nothing was actually
        wrong with them). Retrying sequentially avoids recreating the
        same contention that caused the timeout in the first place.

        IMPORTANT -- a "failure" here can be a false alarm: a read
        timeout means we didn't get the response in time, NOT that the
        request never reached the server. The server may well have
        already completed it. Blindly retrying would then repeat an
        already-successful operation -- harmless for a delete (the
        second attempt just fails with "not found"), but dangerous for
        a create, where it means a duplicate BomItem.

        `filter_pending`, if given, is called with the list of items
        that failed and must return only the ones that genuinely still
        need to be retried (by checking the server for what actually
        happened) -- see add_bom_items_concurrently() and clear_bom()
        for how each of them implements this check.
        """
        failures = self._run_concurrently(items, operation, max_workers)

        attempt = 0
        while failures and attempt < retries:
            attempt += 1
            retry_items = [item for item, _exc in failures]
            if filter_pending is not None:
                retry_items = filter_pending(retry_items)
            failures = self._run_concurrently(retry_items, operation, max_workers=1)

        return failures

    def clear_bom(self, assembly: Part, max_workers: int = 5, retries: int = 1) -> list:
        """Delete every existing BomItem under this assembly, so it can
        be rebuilt from scratch (used by the 'update existing project'
        workflow). Deletions are sent concurrently (with retries) for
        the same reason creates are -- see add_bom_items_concurrently().

        Returns a list of (BomItem, Exception) for any item that could
        not be deleted, so the caller can decide what to do (e.g. abort
        rather than add new items on top of a BOM that wasn't fully
        cleared).
        """
        existing_items = list(BomItem.list(self.api, part=assembly.pk))

        def _delete(item):
            item.delete()

        def _filter_still_present(items):
            # Before retrying a "failed" delete, check which of these
            # items are actually still there -- one that timed out on
            # our end may already be gone on the server.
            current_pks = {i.pk for i in BomItem.list(self.api, part=assembly.pk)}
            return [item for item in items if item.pk in current_pks]

        return self._run_with_retries(
            existing_items, _delete, max_workers, retries,
            filter_pending=_filter_still_present,
        )

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
        way build_name_index() was for reads. Instead, it sends several
        individual create requests IN PARALLEL using a thread pool (with
        retries -- see _run_with_retries() above for why, and for why
        those retries need to check the server first before repeating a
        create that may have actually already gone through).

        `items` is a list of (BomLine, Part) tuples, as produced by
        match_lines() in main.py -- BomLine only needs a `.quantity`
        attribute here, so this method doesn't need to import that
        class.

        Returns a list of (item, Exception) for any item that still
        failed after all retries, so the caller can report exactly
        which ones didn't make it in.
        """
        def _create(item):
            line, part = item
            self.add_bom_item(assembly, part, line.quantity)

        def _filter_still_missing(items):
            # Before retrying a "failed" create, check which of these
            # sub-parts genuinely have no BomItem yet -- one that timed
            # out on our end may have actually been created already.
            existing_sub_parts = {
                bom_item.sub_part for bom_item in BomItem.list(self.api, part=assembly.pk)
            }
            still_missing = []
            for item in items:
                _line, part = item
                if part.pk not in existing_sub_parts:
                    still_missing.append(item)
            return still_missing

        return self._run_with_retries(
            items, _create, max_workers, retries,
            filter_pending=_filter_still_missing,
        )

    # ---------------------------------------------------------------
    # Checking (read-only comparison, never writes anything)
    # ---------------------------------------------------------------

    def get_bom_contents(self, assembly: Part) -> dict:
        """Return the assembly's current BOM as {sub_part_pk: quantity},
        for comparing against a parsed BOM file (see CheckWorker in
        main.py). If the same sub_part somehow appears in more than one
        BomItem row, their quantities are summed."""
        contents: dict = {}
        for bom_item in BomItem.list(self.api, part=assembly.pk):
            contents[bom_item.sub_part] = (
                contents.get(bom_item.sub_part, 0) + bom_item.quantity
            )
        return contents

    def build_pk_index(self) -> dict:
        """Fetch every Part in InvenTree ONCE, indexed by pk -- used only
        for turning a bare part pk (as stored on a BomItem) back into a
        readable name when reporting check results.

        Kept as a separate fetch from build_name_index(), even though
        both ultimately read the same Part.list(), so each method stays
        simple and single-purpose and this new read-only feature can't
        accidentally affect the already-tested import path.
        """
        return {part.pk: part for part in Part.list(self.api)}
