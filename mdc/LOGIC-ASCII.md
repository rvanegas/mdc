# ASCII Logic Notation — Syntax and Semantics

This document defines the ASCII notation for a modal first-order logic extended with quantification over both particulars and universals. It covers every symbol kind, how formulas are formed, and what they mean.

---

## Symbol Kinds

Four kinds of symbol appear in formulas. Case is the only syntactic distinction between the two variable kinds. Within any single formula, the same letter is not used for both an individual variable and a predicate variable (e.g. `x` and `X` do not both appear in one formula) — this avoids visual ambiguity.

| Kind                | Range                                         | Refers to           |
|---------------------|-----------------------------------------------|---------------------|
| Individual variable | `x y z u v w`                                 | a particular        |
| Predicate variable  | `X Y Z U V W`                                 | a universal         |
| Individual constant | `a b c d e f` (then `a1` ...)                 | a named particular  |
| Predicate constant  | `P Q R S T G H I J K L M N O` (then `P1` ...) | a named universal   |

**Particulars** are the entities for which Leibniz's Law fails: two distinct particulars can share all their properties. **Universals** are the entities for which Leibniz's Law holds: if two universals are indiscernible they are identical. This distinction is metaphysical, not syntactic — the logic does not enforce it. Placing a particular where a universal is expected, or vice versa, yields a false formula, not an error.

---

## Atomic Formulas

### Predication

A predicate (constant or variable) followed immediately by zero or more arguments, with no parentheses or separators. Arguments may be individual variables, individual constants, or predicate variables.

```
P          -- zero-argument predicate (propositional)
Pa         -- P applied to constant a
Pxy        -- P applied to x then y
Xab        -- predicate variable X applied to a then b
Xy         -- predicate variable X applied to individual variable y
```

The head and each argument are single symbols; length makes the parse unambiguous given the symbol ranges.

### Identity

Written infix with `=`. Identity refers to the metaphysical relation and is a two-place predicate constant whose arguments may be any symbol (individual or predicate):

```
x = a      -- particular x is identical to constant a
x = Y      -- particular x is identical to universal Y (false for any x, Y)
X = P      -- universal X is identical to universal P
```

---

## Compound Formulas

### Connectives

| Connective    | Symbol    | Arity  |
|---------------|-----------|--------|
| Negation      | `not`     | unary  |
| Conjunction   | `and`     | binary |
| Disjunction   | `or`      | binary |
| Implication   | `implies` | binary |
| Biconditional | `equiv`   | binary |

Precedence (tighter-binding first): `not` > `and` > `or` > `implies` > `equiv`. `implies` is right-associative; `and` and `or` are left-associative. Parentheses are added only when the default parse would give a different tree.

```
not Px
Px and Qx
Px or Qx implies Rx
not Px and Qx             -- reads as (not Px) and Qx
Px implies Qx implies Rx  -- reads as Px implies (Qx implies Rx)
```

### Quantifiers

`forall` and `exists` each bind a comma-separated list of one or more variables, then a dot, then the body formula. Individual variables (lower case) range over particulars; predicate variables (upper case) range over universals. Both kinds may appear in the same binder.

```
forall x. Px
exists x. Px and Qx
forall x,y. Rxy
forall X,y. Xy implies Py
exists X. Xa and not Xb
```

The quantifier has the lowest precedence of all operators: its scope extends as far right as possible. Parentheses are required to close the scope early:

```
forall x. Px and Qx      -- forall x binds the whole conjunction
(forall x. Px) and Qa    -- quantifier scope closed before and
```

Nested quantifiers with different connectives remain nested:

```
forall x. exists y. Rxy
forall X,z. exists y. Xzy
```

### Modal Operators

`nec` (necessity) and `pos` (possibility) are unary prefix operators with the same low precedence as quantifiers:

```
nec Px
pos Px and Qx       -- pos scopes over Px only; reads as (pos Px) and Qx
nec (forall x. Px)  -- parentheses required to scope nec over the quantified formula
nec Px and nec Qx   -- each nec scopes over its own atomic; reads as (nec Px) and (nec Qx)
```

---

## Precedence Summary

Higher number binds more tightly.

| Level | Operators                                | Associativity       |
|-------|------------------------------------------|---------------------|
| 5     | `not` `nec` `pos`                        | right (unary)       |
| 4     | `and`                                    | left                |
| 3     | `or`                                     | left                |
| 2     | `implies`                                | right               |
| 1     | `equiv`                                  | left                |
| 0     | `forall` `exists`                        | scope extends right |

Atomic formulas (predications, identity) always bind most tightly.

---

## Semantics

### What quantifiers say

`forall x. Fx` is true when every particular satisfies F. `exists x. Fx` is true when at least one particular satisfies F. `forall X. Xa` is true when every universal is instantiated by a. `exists X. Xa` is true when at least one universal is instantiated by a.

### Type mismatches

There are no ill-formed formulas, only false ones. If a particular appears where a universal is expected — or the reverse — the formula is simply false:

```
forall X,y. nec not X = y   -- necessarily, no particular is identical to any universal
```

### Predicate variables as first-class

Quantifying over predicate variables is not a higher-order ascent — it is quantification over universals, which are full members of the domain alongside particulars. The case convention marks which sub-domain a bound variable ranges over, not a logical type distinction. `forall X. Xa` says the same kind of thing as `forall x. Px`: it ranges over entities in the domain and says something about each of them.

---

## Examples

```
-- Every particular that is P is also Q
forall x. Px implies Qx

-- There exists a particular that is both P and Q
exists x. Px and Qx

-- Every universal instantiated by a is also instantiated by b
forall X. Xa implies Xb

-- For every particular and every universal, the universal does not apply to the particular necessarily
forall X,y. not nec Xy

-- The categorical necessity: no particular is identical to any universal
forall X,y. nec not X = y

-- P and Q are the same universal (both are necessarily coextensive)
forall x. nec (Px equiv Qx)
```
