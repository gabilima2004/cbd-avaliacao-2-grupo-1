"""Execução reproduzível com captura UTF-8 e código de saída confiável."""
import pathlib
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parents[1]
result = subprocess.run(
    [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
    cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    encoding='utf-8', env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'},
)
text = result.stdout + f'\nEXIT_CODE={result.returncode}\n'
target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else root / 'docs/ia/testes_ultima_execucao.txt'
target.write_text(text, encoding='utf-8')
print(text, end='')
sys.exit(result.returncode)
