from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.errors import RequestFault

MAX_SCRIPT_CHARS = 100_000
MAX_FILE_BYTES = 131_072
MAX_PLAN_LENGTH = 12_000
MAX_TOOL_NAME_CHARS = 200
MAX_TOOL_ARGUMENT_CHARS = 1_048_576
MAX_REASON_QUESTION = 8_000
MAX_REASON_DEPTH = 3
MAX_REASON_BRANCHES = 3
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 180
MAX_EXECUTION_TIMEOUT_SECONDS = 1_800
MAX_PACKAGE_COUNT = 24
PACKAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.+-]{0,99}$")

TOOL_NAMES = (
    "run_shell",
    "run_python",
    "install_packages",
    "write_file",
    "read_file",
    "expose_port",
    "browse_url",
    "update_plan",
    "reason",
    "update_state",
    "delegate_task",
    "consult_advisor",
    "multi_model_consensus",
    "cloud_browser_start",
    "cloud_browser_stop",
)

SANDBOX_TOOLS = frozenset(
    {
        "run_shell",
        "run_python",
        "install_packages",
        "write_file",
        "read_file",
        "expose_port",
        "browse_url",
    }
)


@dataclass(frozen=True)
class StringField:
    description: str
    required: bool = True
    min_length: int = 1
    max_length: int = 4096
    max_bytes: int | None = None
    nul_forbidden: bool = False

    kind = "string"

    def validate(self, value: Any, key: str) -> str:
        if not isinstance(value, str):
            raise RequestFault(f"Hibás argumentum: {key}", code="INVALID_ARGUMENT")
        if len(value) < self.min_length or len(value) > self.max_length:
            raise RequestFault(f"Hibás argumentum: {key}", code="INVALID_ARGUMENT")
        if self.min_length > 0 and not value.strip():
            raise RequestFault(f"Hibás argumentum: {key}", code="INVALID_ARGUMENT")
        if self.nul_forbidden and "\x00" in value:
            raise RequestFault(f"Hibás argumentum: {key}", code="INVALID_ARGUMENT")
        if self.max_bytes is not None and len(value.encode("utf-8")) > self.max_bytes:
            raise RequestFault(f"Hibás argumentum: {key}", code="INVALID_ARGUMENT")
        return value


@dataclass(frozen=True)
class IntegerField:
    description: str
    required: bool = True
    minimum: int = 0
    maximum: int = 0
    default_value: int | None = None

    kind = "integer"

    def validate(self, value: Any, key: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise RequestFault(f"Hibás egész szám: {key}", code="INVALID_ARGUMENT")
        if value < self.minimum or value > self.maximum:
            raise RequestFault(f"Hibás egész szám: {key}", code="INVALID_ARGUMENT")
        return value


@dataclass(frozen=True)
class StringArrayField:
    description: str
    required: bool = True
    min_items: int = 1
    max_items: int = 1
    item_pattern: re.Pattern[str] = field(default=re.compile(r"^.*$"))

    kind = "string_array"

    def validate(self, value: Any, key: str) -> list[str]:
        if not isinstance(value, list):
            raise RequestFault(f"Hibás lista: {key}", code="INVALID_ARGUMENT")
        if len(value) < self.min_items or len(value) > self.max_items:
            raise RequestFault(f"Hibás lista: {key}", code="INVALID_ARGUMENT")
        for item in value:
            if not isinstance(item, str) or not self.item_pattern.match(item):
                raise RequestFault(f"Hibás lista: {key}", code="INVALID_ARGUMENT")
        return list(value)


FieldSpec = StringField | IntegerField | StringArrayField


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    fields: dict[str, FieldSpec]
    require_any: bool = False


def _string(
    description: str,
    max_length: int,
    required: bool = True,
    nul_forbidden: bool = False,
) -> StringField:
    return StringField(
        description=description,
        required=required,
        min_length=1 if required else 0,
        max_length=max_length,
        nul_forbidden=nul_forbidden,
    )


def _integer(
    description: str,
    minimum: int,
    maximum: int,
    required: bool = True,
    default_value: int | None = None,
) -> IntegerField:
    return IntegerField(
        description=description,
        required=required,
        minimum=minimum,
        maximum=maximum,
        default_value=default_value,
    )


TOOL_SPECS: dict[str, ToolSpec] = {
    "run_shell": ToolSpec(
        name="run_shell",
        description=(
            "Parancs futtatása a feladathoz rendelt izolált Linux VM-ben. A fájlok és telepített "
            "eszközök a VM élettartamán belül megmaradnak; a parancsok önálló shell-folyamatok."
        ),
        fields={
            "command": _string("Bash parancs", MAX_SCRIPT_CHARS),
            "timeout": _integer(
                "Végrehajtási határidő másodpercben, alapérték 180, maximum 1800",
                1,
                MAX_EXECUTION_TIMEOUT_SECONDS,
                required=False,
                default_value=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
            ),
        },
    ),
    "run_python": ToolSpec(
        name="run_python",
        description=(
            "Python 3 program futtatása a feladathoz rendelt Linux VM-ben; a Python értelmező "
            "változói az új hívások között nem maradnak meg."
        ),
        fields={
            "code": _string("Python 3 forráskód", MAX_SCRIPT_CHARS),
            "timeout": _integer(
                "Végrehajtási határidő másodpercben, alapérték 180, maximum 1800",
                1,
                MAX_EXECUTION_TIMEOUT_SECONDS,
                required=False,
                default_value=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
            ),
        },
    ),
    "install_packages": ToolSpec(
        name="install_packages",
        description=(
            "Nix csomagok telepítése a saját VM-ben. A csomagok elérhetősége a Nix csatornától, "
            "a hálózattól és a telepítés sikerétől függ."
        ),
        fields={
            "packages": StringArrayField(
                description="Érvényes nixpkgs attribútumnevek",
                min_items=1,
                max_items=MAX_PACKAGE_COUNT,
                item_pattern=PACKAGE_PATTERN,
            )
        },
    ),
    "write_file": ToolSpec(
        name="write_file",
        description=(
            "UTF-8 szöveges fájl atomi írása és SHA-256 ellenőrzése a VM-ben; legfeljebb 128 KiB "
            "(131072 bájt) UTF-8 kódolásban."
        ),
        fields={
            "path": _string("Fájl elérési útja", 4096, nul_forbidden=True),
            "content": StringField(
                description="UTF-8 fájl tartalma, lehet üres, legfeljebb 131072 bájt",
                required=True,
                min_length=0,
                max_length=MAX_FILE_BYTES,
                max_bytes=MAX_FILE_BYTES,
            ),
        },
    ),
    "read_file": ToolSpec(
        name="read_file",
        description=(
            "Fájl bájtszintű lapozása; a válasz Base64-kódolt adatot, bytes_read, next_offset és "
            "eof mezőket tartalmaz. Egy lap 6000 bájt."
        ),
        fields={
            "path": _string("Fájl elérési útja", 4096, nul_forbidden=True),
            "offset": _integer(
                "Nemnegatív, biztonságos egész bájteltolás",
                0,
                2**53 - 1,
                required=False,
                default_value=0,
            ),
        },
    ),
    "expose_port": ToolSpec(
        name="expose_port",
        description=(
            "A VM-en futó TCP szolgáltatás ellenőrzése és a feladat ideje alatt elérhető InstaVM "
            "port URL lekérése. A VM a feladat végén megszűnik."
        ),
        fields={"port": _integer("TCP port", 1, 65535)},
    ),
    "browse_url": ToolSpec(
        name="browse_url",
        description=(
            "Valódi távoli böngészővel oldal navigálása és tartalmának lekérdezése, az engedélyezett "
            "HTTPS gazdagépekre korlátozva."
        ),
        fields={"url": _string("Engedélyezett HTTPS URL", 4096)},
    ),
    "update_plan": ToolSpec(
        name="update_plan",
        description=(
            "A feladat aktuális tervének és mérföldköveinek rögzítése a futó feladat állapotába."
        ),
        fields={"plan": _string("Teljes aktuális terv", MAX_PLAN_LENGTH)},
    ),
    "reason": ToolSpec(
        name="reason",
        description=(
            "Izolált rekurzív következtetés: összetett alproblémát fába bont, ágankénti "
            "alkérdéseket önállóan megvizsgál és összefoglal, a feladat globális állapotának "
            "módosítása nélkül."
        ),
        fields={
            "question": _string("A vizsgálandó összetett kérdés", MAX_REASON_QUESTION),
            "depth": _integer(
                "Rekurziós mélység 1-3", 1, MAX_REASON_DEPTH, required=False, default_value=2
            ),
            "branches": _integer(
                "Ágak száma szintenként 1-3",
                1,
                MAX_REASON_BRANCHES,
                required=False,
                default_value=2,
            ),
        },
    ),
    "update_state": ToolSpec(
        name="update_state",
        description=(
            "A munkamemória (Σ) frissítése: tény rögzítése, blokkoló felvétele vagy feloldása, "
            "részcél lezárása azonosító alapján. Legalább egy nem üres mezőt meg kell adni."
        ),
        require_any=True,
        fields={
            "fact": _string("Új ellenőrzött tény", 400, required=False),
            "blocker": _string("Új blokkoló", 400, required=False),
            "resolve_blocker": _string("Feloldott blokkoló szövegrészlete", 400, required=False),
            "complete_subgoal": _string("Lezárt részcél azonosítója", 100, required=False),
        },
    ),
    "delegate_task": ToolSpec(
        name="delegate_task",
        description=(
            "Részfeladat kiadása gyors Gemini alügynöknek, amely önállóan kódot futtat, "
            "Google-ben keres és URL-eket olvas, majd ellenőrzött eredményt ad vissza."
        ),
        fields={
            "task": _string("A pontosan megfogalmazott részfeladat", 32_000),
            "context": _string("A részfeladathoz szükséges háttér", 32_000, required=False),
        },
    ),
    "consult_advisor": ToolSpec(
        name="consult_advisor",
        description=(
            "Erősebb Gemini modell megkérdezése tanácsért: elemzi a helyzetet, hibákat és "
            "kockázatokat jelez, és konkrét továbblépést javasol."
        ),
        fields={
            "question": _string("A tanácsadónak feltett kérdés", 16_000),
            "context": _string("Az eddigi állapot, próbálkozások és eredmények", 32_000, required=False),
        },
    ),
    "multi_model_consensus": ToolSpec(
        name="multi_model_consensus",
        description=(
            "Ugyanazt a kérdést több Gemini modell egymástól függetlenül megválaszolja, majd egy "
            "erős modell összeveti a válaszokat és konszenzust, eltéréseket és bizonyossági "
            "szintet ad."
        ),
        fields={
            "question": _string("Az elemzendő kérdés", 16_000),
            "context": _string("Háttérinformáció", 32_000, required=False),
        },
    ),
    "cloud_browser_start": ToolSpec(
        name="cloud_browser_start",
        description=(
            "Felügyelt Chrome indítása a Browser Use Cloudban proxyval, felvétellel, legfeljebb 4 "
            "órára. Visszaadja a CDP websocket címet és az élő nézet URL-jét."
        ),
        fields={
            "country_code": _string("Proxy ország kód, alapértelmezés us", 8, required=False),
            "timeout_minutes": _integer(
                "Futási idő percben", 1, 240, required=False, default_value=240
            ),
        },
    ),
    "cloud_browser_stop": ToolSpec(
        name="cloud_browser_stop",
        description=(
            "A felhőböngésző leállítása és a felvétel URL-jének lekérése. Minden elindított "
            "böngészőt állíts le, ha végeztél vele."
        ),
        fields={"browser_id": _string("A cloud_browser_start által visszaadott azonosító", 200)},
    ),
}


def is_tool_name(name: str) -> bool:
    return name in TOOL_SPECS


def validate_arguments(name: str, raw: Any) -> dict[str, Any]:
    if not is_tool_name(name):
        raise RequestFault("Ismeretlen eszköz.", code="UNKNOWN_TOOL")
    if not isinstance(raw, dict):
        raise RequestFault(
            "Az eszköz argumentumainak JSON objektumnak kell lenniük.",
            code="INVALID_ARGUMENT",
        )
    spec = TOOL_SPECS[name]
    validated: dict[str, Any] = {}
    for key in raw:
        if key not in spec.fields:
            raise RequestFault(f"Ismeretlen argumentum: {key}", code="INVALID_ARGUMENT")
    for key, field_spec in spec.fields.items():
        if key not in raw:
            if field_spec.required:
                raise RequestFault(f"Hiányzó argumentum: {key}", code="INVALID_ARGUMENT")
            if isinstance(field_spec, IntegerField) and field_spec.default_value is not None:
                validated[key] = field_spec.default_value
            continue
        validated[key] = field_spec.validate(raw[key], key)
    if spec.require_any:
        has_value = False
        for value in validated.values():
            if isinstance(value, str) and value.strip():
                has_value = True
                break
            if isinstance(value, list) and value:
                has_value = True
                break
            if isinstance(value, int):
                has_value = True
                break
        if not has_value:
            raise RequestFault(
                "Legalább egy nem üres argumentum megadása kötelező.",
                code="INVALID_ARGUMENT",
            )
    return validated


def provider_tools(names: list[str]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name in names:
        spec = TOOL_SPECS[name]
        properties: dict[str, Any] = {}
        required: list[str] = []
        for key, field_spec in spec.fields.items():
            if field_spec.required:
                required.append(key)
            if isinstance(field_spec, StringField):
                properties[key] = {
                    "type": "string",
                    "minLength": field_spec.min_length,
                    "maxLength": field_spec.max_length,
                    "description": field_spec.description,
                }
            elif isinstance(field_spec, IntegerField):
                properties[key] = {
                    "type": "integer",
                    "minimum": field_spec.minimum,
                    "maximum": field_spec.maximum,
                    "description": field_spec.description,
                }
            elif isinstance(field_spec, StringArrayField):
                properties[key] = {
                    "type": "array",
                    "minItems": field_spec.min_items,
                    "maxItems": field_spec.max_items,
                    "items": {"type": "string", "pattern": field_spec.item_pattern.pattern},
                    "description": field_spec.description,
                }
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        if spec.require_any:
            parameters["minProperties"] = 1
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.description,
                    "parameters": parameters,
                },
            }
        )
    return tools


def enabled_tool_names(insta_key: str | None, browser_host_count: int) -> list[str]:
    names: list[str] = []
    for name in TOOL_NAMES:
        if name in SANDBOX_TOOLS:
            if insta_key is None:
                continue
            if name == "browse_url" and browser_host_count == 0:
                continue
        names.append(name)
    return names


def tool_action_text(name: str) -> str:
    return TOOL_ACTIONS.get(name, "Végrehajtom a következő műveletet.")


TOOL_ACTIONS: dict[str, str] = {
    "run_shell": "Parancsot futtatok a virtuális gépen.",
    "run_python": "Python-műveletet futtatok a virtuális gépen.",
    "browse_url": "Megnyitom és átnézem a weboldalt.",
    "cloud_browser_start": "Elindítom a felhőböngészőt.",
    "cloud_browser_stop": "Leállítom a felhőböngészőt és lekérem a felvételt.",
    "delegate_task": "Egy segítő modell dolgozik a részfeladaton.",
    "consult_advisor": "Egy segítő modell véleményét kérem az elakadásról.",
    "multi_model_consensus": "Összevetem több modell megállapításait.",
    "write_file": "Elmentem az elkészült fájlt.",
    "read_file": "Átnézem a fájl tartalmát.",
    "reason": "Részletesen mérlegelem a következő döntést.",
    "update_state": "Frissítem a részcélok állapotát.",
    "update_plan": "Frissítem a munkatervet.",
    "install_packages": "Telepítem a szükséges programokat.",
    "expose_port": "Elérhetővé teszem az elkészült szolgáltatást.",
}
