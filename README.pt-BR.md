# csv-sqlite-import

Converta um CSV local em um **novo banco SQLite**, com tipos explícitos opcionais,
hash SHA-256 da origem e publicação atômica sem sobrescrever o destino. Requer
Python 3.10+ e usa somente a biblioteca padrão. Não precisa de instalação,
serviço, conta nem rede.

[Read in English](README.md).

## Uso rápido

Execute na pasta deste repositório:

```sh
python -m csv_sqlite_import examples/inventory.csv inventory.sqlite3 \
  --table inventory --schema examples/inventory.schema.json
```

A saída é um objeto JSON com `table`, `rows`, `columns` e `sha256`. O exemplo
contém três registros fictícios de estoque, uma vírgula entre aspas e uma nota
com várias linhas. Omita `--schema` para manter todos os campos como texto.

```python
from pathlib import Path
from csv_sqlite_import import CsvImportError, import_csv

try:
    resumo = import_csv(
        Path("examples/inventory.csv"),
        Path("inventory-text.sqlite3"),
        table="inventory",
    )
except CsvImportError:
    print("Não foi possível importar a entrada.")
else:
    print(resumo)
```

`source` e `destination` aceitam strings e objetos `os.PathLike` que retornem
strings. Informe caminhos para arquivos locais. A ferramenta não expande
caminhos de shell, não procura arquivos, não faz downloads e não modifica a
origem. Caminhos relativos usam a pasta atual do processo. O destino não pode
existir, nem mesmo como link simbólico quebrado.

## Tipos e esquemas

O esquema deve ter exatamente esta estrutura. A ordem e os nomes das colunas
devem corresponder exatamente ao cabeçalho do CSV:

```json
{
  "columns": [
    {"name": "label", "type": "text", "nullable": false},
    {"name": "count", "type": "integer", "nullable": true}
  ]
}
```

Cada coluna exige apenas `name`, `type` e `nullable`; chaves extras são inválidas.
A API aceita somente dicionários, listas, strings e booleanos nativos exatos
nessa estrutura. `nullable` deve ser booleano, não `0`, `1` ou uma string. A CLI
rejeita chaves JSON duplicadas, constantes não finitas e esquemas maiores que
64 KiB. O arquivo de esquema deve ser um arquivo regular JSON em UTF-8.
Esquemas são dados passivos: não há avaliação de expressões, callbacks,
inferência de datas, conversão regional, extensões ou execução de código.

| Tipo | Entrada aceita | Armazenamento no SQLite |
| --- | --- | --- |
| `text` | Campo original, preservando espaços | `TEXT` |
| `integer` | ASCII `-?(0\|[1-9][0-9]*)`, inteiro assinado de 64 bits | `INTEGER` |
| `real` | Gramática numérica JSON, finito após conversão | `REAL` |
| `boolean` | Somente `true` ou `false` | `INTEGER` 1 ou 0 |
| `decimal` | Gramática numérica JSON, preservada exatamente | `TEXT` |

A gramática numérica JSON é
`-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?`. Ela rejeita sinal de mais no
início, zeros à esquerda, espaços, `NaN` e infinito. O tipo `real` usa ponto
flutuante binário do Python e pode arredondar ou sofrer underflow. Use `decimal`
para preservar a grafia original, os zeros finais e o expoente. Como `decimal`
é texto, a ordenação no SQLite será textual sem conversão explícita na consulta.

Em colunas anuláveis, um campo vazio vira SQL `NULL`, inclusive em `text`. Em
`text` não anulável, o vazio permanece `""`. Os demais tipos não anuláveis
rejeitam o vazio. Campos vazios com e sem aspas têm o mesmo comportamento.
Sem esquema, todas as colunas são texto não anulável, sem inferência ou remoção
de espaços.

## Regras e limites do CSV

- A origem deve ser um arquivo regular UTF-8 de até **64 MiB**, com BOM UTF-8
  opcional. Os bytes são lidos uma vez para um snapshot limitado em memória.
  A decodificação e a análise precisam de memória adicional; a ferramenta não
  oferece streaming ilimitado.
- O SHA-256 corresponde aos bytes exatos importados, incluindo BOM, finais de
  linha e aspas, sem normalização.
- A análise usa `csv.reader(strict=True)` com `newline=''`, preservando vírgulas
  entre aspas, quebras de linha dentro de campos e espaços.
- Os delimitadores aceitos são vírgula, tabulação e ponto e vírgula. Na API,
  use `','`, `'\t'` ou `';'`; na CLI, `--delimiter comma|tab|semicolon`.
- O cabeçalho deve ter de 1 a 1.000 nomes não vazios de até 128 caracteres, sem
  NUL nem duplicatas após Unicode case folding. Os nomes não são aparados.
  Um arquivo apenas com cabeçalho cria uma tabela vazia.
- São permitidos até 1.000.000 de registros e 65.536 caracteres por campo.
  Cada registro deve ter a mesma quantidade de campos do cabeçalho. Registros
  completamente em branco são inválidos.
- O `csv.field_size_limit()` global nunca é alterado. Um limite menor definido
  pelo processo pode rejeitar campos menores que o limite desta ferramenta.
- O nome da tabela deve corresponder a ASCII `[A-Za-z_][A-Za-z0-9_]{0,62}` e não
  pode começar com `sqlite_`, sem diferenciar maiúsculas. Nomes de colunas podem
  conter pontuação e texto semelhante a SQL; todos os identificadores são
  delimitados e todos os valores usam parâmetros vinculados.

## Publicação e falhas

A importação cria um banco temporário com modo 0600 na pasta do destino. A
criação da tabela e a inserção dos registros usam uma transação única, seguida
de `PRAGMA integrity_check`, commit e fechamento do SQLite. A publicação usa
`os.link`, que falha atomicamente se já houver qualquer entrada no destino.
Depois, o banco temporário e seus próprios arquivos de journal são removidos.

CSV, esquema ou tipos inválidos e falhas comuns de E/S ou SQLite geram
`CsvImportError`, subclasse de `ValueError`. As mensagens não mostram caminhos,
SQL nem conteúdo das células. Uma falha não publica um banco. Arquivos, pastas,
links simbólicos e links quebrados existentes permanecem intactos. Em
importações concorrentes para o mesmo destino, apenas uma pode vencer.

É necessário um sistema de arquivos local confiável com suporte a hard links e
uma pasta de destino já existente. Não há garantia de durabilidade em uma queda
de energia, proteção contra um agente hostil que altere a pasta de destino nem
consistência caso outro processo reescreva a origem durante a leitura. Não há
modo de anexar, atualizar, sobrescrever ou abrir um banco de destino existente,
nem carregamento de triggers, extensões ou provedores.

A CLI retorna status 0 e JSON em stdout no sucesso. Em caso de erro, retorna
status 2, mensagem genérica em stderr e nenhum JSON. `--help` retorna status 0.

## Desenvolvimento

```sh
python -m unittest discover -s tests -v
```

Os testes públicos usam pastas temporárias e dados fictícios. Cobrem concorrência
na publicação, rollback e limpeza, restrições de arquivos, validação de tipos e
esquemas, hashes exatos, cabeçalhos semelhantes a SQL, aspas, BOM e campos com
várias linhas. O GitHub Actions executa os testes no Python 3.10, 3.12 e 3.14,
com permissões de leitura e credenciais de checkout desativadas. Não há
dependências externas.

MIT © 2026 Martin Lofrano. Consulte [LICENSE](LICENSE).
