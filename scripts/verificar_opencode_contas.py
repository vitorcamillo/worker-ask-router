"""Mutantes do delta de rotacao de contas Go: cada um precisa derrubar tests -k rotacao."""
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
FONTE = (RAIZ / "bin" / "opencode-ask").read_text()
MUTANTES = {
    "sem_switch": ('"$OC" auth switch opencode-go "$proxima"', "true"),
    "sem_pre": ('gira_conta pre "$_ativa_pre" || true', "true"),
    "sem_laco": ('[ "$trocas_contas" -lt 3 ]', '[ "$trocas_contas" -lt 0 ]'),
    "ignora_desliga": ('[ "${OPENCODE_ASK_ACCOUNT_ROTATE:-1}" = 0 ] && return 1', "false"),
}
vivos = []
for nome, (de, para) in MUTANTES.items():
    assert FONTE.count(de) == 1, nome
    with tempfile.TemporaryDirectory() as d:
        alvo = Path(d) / "opencode-ask"
        alvo.write_text(FONTE.replace(de, para))
        alvo.chmod(0o755)
        env = dict(os.environ, GUARDS_SRC=d, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        rc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-k", "rotacao", str(RAIZ / "tests" / "test_opencode.py")],
                            env=env, capture_output=True).returncode
    print(nome, "morto" if rc else "VIVO")
    if not rc:
        vivos.append(nome)
PIT = RAIZ / "out-opencode-contas" / "target" / "pit-reports" / "mutations.xml"
PIT.parent.mkdir(parents=True, exist_ok=True)
rep = ET.Element("mutations")
for nome in MUTANTES:
    ET.SubElement(rep, "mutation", {"detected": str(nome not in vivos).lower(), "mutator": nome})
ET.ElementTree(rep).write(PIT, encoding="utf-8", xml_declaration=True)
print(json.dumps({"mutation_score_pct": round(100 * (len(MUTANTES) - len(vivos)) / len(MUTANTES), 1)}))
sys.exit(1 if vivos else 0)
