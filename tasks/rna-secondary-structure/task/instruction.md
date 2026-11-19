We need a program that predicts an RNA's secondary structure, meaning which bases pair with which, from its sequence. We'll mostly run it on RNAs from families nobody has characterised yet, so it has to hold up on families unlike anything in our data.

What we have is in `data/` (format in `data/README.md`):

- `train.parquet`: about 57,800 RNAs with known structures, each tagged with its Rfam family and clan.
- `sequences.fa.gz`: about 285,000 more RNA sequences from many families, without structures.

The program is `./predict <in.jsonl> <out.jsonl>`, run from the repo root. Each input line is `{"id": "...", "sequence": "..."}`, with up to 1,000 nucleotides (A, C, G, U, occasionally other IUPAC letters). For each line, write `{"id": "...", "structure": "..."}`: a dot-bracket string the same length as the sequence. Use `()` for ordinary pairs and `[]`, `{}`, `<>` for pairs that cross them. The sequences in one input file come from different families, so predict each one on its own.

We'll run it on our processing server, which has the same setup and `data/` but no internet access, on batches of RNAs from families that aren't in `train.parquet`. We deploy from `main`, so commit everything it needs beyond `data/`, trained models included, and keep `main` working as you go.

A prediction is judged by how many of the true base pairs it gets exactly right, balanced against pairs it predicts that aren't there (F1 for each RNA, averaged within each family and then across families). Make it as accurate as you can; we'll deploy whatever is on `main` four hours after you start.
