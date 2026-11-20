# data/

## train.parquet

One row per RNA.

| column | type | meaning |
|---|---|---|
| `id` | string | unique id of the RNA |
| `sequence` | string | the RNA sequence, 5' to 3', upper case. Mostly `A`, `C`, `G`, `U`; occasionally other IUPAC letters (`N`, `R`, `Y`, ...) |
| `structure` | string | its secondary structure in dot-bracket, the same length as `sequence`. `.` is unpaired; `(` `)` mark nested pairs, and `[` `]`, `{` `}`, `<` `>` mark pairs that cross them (pseudoknots). Each opening bracket pairs with the matching closing bracket of the same kind |
| `family` | string | Rfam family accession, for example `RF00005` |
| `family_name` | string | Rfam family id, for example `tRNA` |
| `clan` | string or null | Rfam clan accession, for example `CL00001`; null if the family is in no clan |
| `rna_type` | string | Rfam's type for the family, for example `Gene; tRNA;` |

Families related to each other are grouped into clans; members of one family share a structure,
with variation.

## sequences.fa.gz

More RNA sequences, without structures: gzipped FASTA, one record per RNA, in no particular order.

```
>s0000001
GAGCAGUGAUGAAAAGGUUUUACUACUGACCUUUACAACUAUGAAGG...
```

- The header line is `>` and an id. Ids are arbitrary labels that carry no information.
- The sequence follows on a single line, 5' to 3', upper case. Mostly `A`, `C`, `G`, `U`; occasionally
  other IUPAC letters (`N`, `R`, `Y`, ...). Lengths run from about 20 to about 4,900 nucleotides.
- There is nothing else: no structures, families or other annotations.
