# Logic Sentence Grammar

This document defines the syntax for logical formulas used in Dianoia. Formulas are expressed in two equivalent forms: **ASCII** (text) and **JSON** (structured data). Both are produced and consumed by `src/core/logic.py`.

The language is modal first-order logic. Propositional logic is a subset: use only zero-argument predicates and connectives, with no quantifiers, identity, or terms.

---

## Canonical Symbol Ranges

The alphabet is partitioned so each symbol kind occupies a non-overlapping range.

| Kind      | ASCII example | JSON `"type"`  | Canonical names                                   | Overflow              |
|-----------|---------------|----------------|---------------------------------------------------|-----------------------|
| Variable  | `x`           | `"variable"`   | `x y z u v w`                                     | none (max 6)          |
| Constant  | `a`           | `"constant"`   | `a b c d e f`                                     | `a1-f1`, `a2-f2`, ... |
| Predicate | `P`           | `"predicate"`  | `P Q R S T G H I J K L M N O`                     | `P1-O1`, `P2-O2`, ... |

**Variables** (`u-z`, enumerated x, y, z, u, v, w): bound by quantifiers; at most 6 per formula.

**Constants** (`a-f`): individual objects; 6 base slots, then suffixed (`a1`, `b1`, ...).

**Predicates** (`G-T`, enumerated P, Q, R, S, T, G, H, I, J, K, L, M, N, O): 14 base slots, then suffixed (`P1`, `Q1`, ...).

---

## Atomic Formulas

### Predicate application

A predicate applied to zero or more terms. Zero-argument predicates omit the parentheses.

```
ASCII: P(x, a)
JSON:  {
         "type": "predicate",
         "name": "P",
         "args": [
           {"type": "variable", "name": "x"},
           {"type": "constant", "name": "a"}
         ]
       }
```

```
ASCII: P
JSON:  {"type": "predicate", "name": "P", "args": []}
```

### Identity

```
ASCII: x = a
JSON:  {
         "type": "identity",
         "left":  {"type": "variable", "name": "x"},
         "right": {"type": "constant", "name": "a"}
       }
```

---

## Compound Formulas

### Connectives

Negation, conjunction, disjunction, implication, and biconditional are all connectives. The `op` value doubles as the ASCII token. `not` takes one argument; the binary connectives take two. Parentheses are added only when required by precedence.

```
ASCII: not P(x)
JSON:  {"type": "connective", "op": "not", "args": [{"type": "predicate", "name": "P", "args": [...]}]}
```

```
ASCII: P(x) and Q(x)
JSON:  {
         "type": "connective",
         "op": "and",
         "args": [
           {"type": "predicate", "name": "P", "args": [{"type": "variable", "name": "x"}]},
           {"type": "predicate", "name": "Q", "args": [{"type": "variable", "name": "x"}]}
         ]
       }
```

```
ASCII: P(a) implies Q(a)
JSON:  {"type": "connective", "op": "implies", "args": [..., ...]}
```

```
ASCII: P(a) equiv Q(a)
JSON:  {"type": "connective", "op": "equiv", "args": [..., ...]}
```

### Quantifiers

The `quant` value doubles as the ASCII token.

```
ASCII: forall x. P(x)
JSON:  {
         "type": "quantifier",
         "quant": "forall",
         "var":  {"type": "variable", "name": "x"},
         "body": {"type": "predicate", "name": "P", "args": [{"type": "variable", "name": "x"}]}
       }
```

Nested quantifiers:
```
ASCII: forall x. exists y. R(x, y)
JSON:  {
         "type": "quantifier", "quant": "forall",
         "var": {"type": "variable", "name": "x"},
         "body": {
           "type": "quantifier", "quant": "exists",
           "var": {"type": "variable", "name": "y"},
           "body": {
             "type": "predicate", "name": "R",
             "args": [
               {"type": "variable", "name": "x"},
               {"type": "variable", "name": "y"}
             ]
           }
         }
       }
```

### Modal operators

The `mod` value doubles as the ASCII token.

```
ASCII: nec P(x)
JSON:  {"type": "modal", "mod": "nec", "body": {...}}

ASCII: pos P(x)
JSON:  {"type": "modal", "mod": "pos", "body": {...}}
```

---

## Precedence

Higher number binds more tightly. Within the same level, `and`/`or` are left-associative; `implies` is right-associative; unary operators associate right-to-left.

| Level | Operators                        | Note                |
|-------|----------------------------------|---------------------|
| 5     | `not`                            | unary               |
| 4     | `and`                            | left-assoc          |
| 3     | `or`                             | left-assoc          |
| 2     | `implies`                        | right-assoc         |
| 1     | `equiv`                          | left-assoc          |
| 0     | `nec`, `pos`, `forall`, `exists` | scope extends right |

Parentheses in ASCII are emitted only when required by these rules.

---

## Grammar Summary (BNF)

```
formula    ::= predicate
             | identity
             | connective
             | quantifier
             | modal

predicate  ::= NAME "(" term_list ")"
             | NAME                        -- zero-argument: parens omitted
term_list  ::= term ("," term)*

identity   ::= term "=" term

connective ::= "not" formula
             | formula ("and" | "or" | "implies" | "equiv") formula

quantifier ::= ("forall" | "exists") VAR "." formula

modal      ::= ("nec" | "pos") formula

term     ::= VAR | CONST
VAR      ::= "x" | "y" | "z" | "u" | "v" | "w"        -- u-z, enumerated from x
CONST    ::= "a"..."f" | "a1"..."f1" | ...            -- a-f
NAME     ::= "P"..."O" | "P1"..."O1" | ...            -- G-T, enumerated from P
```

---

## JSON Node Reference

| `"type"` value | Required fields                                                             | Optional / notes                                                      |
|----------------|-----------------------------------------------------------------------------|-----------------------------------------------------------------------|
| `"predicate"`  | `name: string`, `args: term[]`                                              | empty `args` omits parens in ASCII                                    |
| `"identity"`   | `left: term`, `right: term`                                                 |                                                                       |
| `"connective"` | `op: "not"\|"and"\|"or"\|"implies"\|"equiv"`, `args: formula[]`             | `not` takes 1; others take 2                                          |
| `"quantifier"` | `quant: "forall"\|"exists"`, `var: term{type:"variable"}`, `body: formula`  |                                                                       |
| `"modal"`      | `mod: "nec"\|"pos"`, `body: formula`                                        |                                                                       |
| `"variable"`   | `name: string`                                                              | canonical: `x y z u v w`; used inside `args`, `var`, `left`, `right`  |
| `"constant"`   | `name: string`                                                              | canonical: `a-f`; used inside `args`, `left`, `right`                 |

---

## Full Example

**ASCII:**
```
forall x. pos P(x) and x = a
```

**JSON:**
```json
{
  "type": "quantifier",
  "quant": "forall",
  "var": {"type": "variable", "name": "x"},
  "body": {
    "type": "modal",
    "mod": "pos",
    "body": {
      "type": "connective",
      "op": "and",
      "args": [
        {
          "type": "predicate",
          "name": "P",
          "args": [{"type": "variable", "name": "x"}]
        },
        {
          "type": "identity",
          "left":  {"type": "variable", "name": "x"},
          "right": {"type": "constant", "name": "a"}
        }
      ]
    }
  }
}
```

---

## Normalization

The `formalization_normalizer` replaces semantic names (e.g. `is_mortal`, `socrates`)
with canonical symbols before storing or displaying a formalization:

- **Predicates** -> `P`, `Q`, `R`, `S`, `T`, `G`, `H`, `I`, ... (G-T range, enumerated from P) in first-appearance order across all steps.
- **Constants** -> `a`, `b`, `c`, ... `f` (a-f range) in first-appearance order.
- **Bound variables** -> `x`, `y`, `z`, `u`, `v`, `w` in DFS quantifier order, independently per formula.

The `ascii` field in stored formalizations is always regenerated from the normalized JSON tree,
never taken verbatim from the model output.
