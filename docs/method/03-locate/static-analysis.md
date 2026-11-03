# Focused static analysis

## Choose this route when

Database and trace evidence leave a concrete ambiguity: a record layout, table selector, request builder, named-event behavior, bit numbering, or routing rule. Do not begin by decompiling an entire application.

## Procedure

1. Write one falsifiable question and list the evidence already known.
2. Hash and record the exact binary version; keep the binary and analysis project private.
3. Start from stable strings, exports, symbols, registry keys, database table names, or a trace-visible API call.
4. Follow only the call/data path needed to answer the question.
5. Cross-check offsets or behavior against multiple records or a live trace.
6. Record the conclusion, function/address references, version/hash, assumptions, and a reproduction recipe in prose or an original script.

Static analysis can make a strong Candidate and explain acquisition construction, but does not establish live support or semantics. Do not redistribute OEM binaries, decompiler output, or analysis databases. Continue through controlled verification.
