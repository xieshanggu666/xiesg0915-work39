"""支持 ``python -m ifc_audit ...``。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
