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
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

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

    def __init__(self, config: InventreeConfig):
        self.api = InvenTreeAPI(
            config.server, username=config.username, password=config.password
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

    def find_part_by_ipn(self, ipn: str) -> Optional[Part]:
        """Look up a single existing Part whose IPN matches exactly.

        Returns None if no part has this IPN.

        Raises LookupError if more than one Part shares the IPN --
        InvenTree does not enforce IPN uniqueness by default, so this
        can genuinely happen and must not be silently guessed at.
        """
        matches = Part.list(self.api, IPN=ipn)
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
