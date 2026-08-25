# Measuring the Act! -> SEC identity gate

**Status: measurement, not a change.** Nothing in `src/` was modified. The rules
below are proposals; the recommendation at the end is the one worth applying.

## The problem this fixes

Four versions of the first-name gate in `src/act_crosswalk.py` produced four
demotion counts -- 721, 2,333, 816, 1,126 -- and nobody could say which was
closest to right, because "right" had no referent. A count cannot be checked
against another count: two rules can demote the same number of rows and
disagree about every row in them. Measured here: rules (a) and (f) demote 1,126
and 851, and they disagree about **275** high-tier rows, which is more than the
gap between the counts.

So this builds the missing referent: a labelled sample whose labels come from
evidence that owes nothing to the Act! name the gate is judging.

## What was built

| file | what it does |
| --- | --- |
| `scripts/gate_eval.py` | runs any candidate gate over all 41,594 matched rows; reports demotions, rule-vs-rule disagreement, a verdict on every anchor case, and precision/recall against the labelled sample. `--census` repeats the evidence test over every matched row; `--errors d` names the rows a rule gets wrong. |
| `scripts/gate_label.py` | builds the labelled sample. |
| `data/interim/gate_truth.csv` | 493 rows, 234 labelled SAME/DIFFERENT, 259 UNKNOWN, each with the evidence string that decided it. |

```
python scripts/gate_label.py                          # rebuild the truth file
python scripts/gate_eval.py --census --errors a,d,e   # every number in this doc
```

## What counts as evidence

The label answers one question: **is this Act! contact the same human as the SEC
advisor this row is matched to?** It may never be answered from the Act! name,
because the Act! name is what is on trial. A truth set built from the names
would agree with any name-comparison rule by construction.

| witness | independent of the Act! name? | how it is used |
| --- | --- | --- |
| SEC filing (`advisors.parquet`: `first_name`, `middle_name`, `used_first_name`) | yes -- `parse_advisors.py` is its sole writer, straight off the SEC XML | the identity being matched to |
| Act! email local part | yes -- the firm assigns the mailbox; the CRM name field is typed by a rep | **decides** SAME / DIFFERENT |
| Act! `businessPhone` -> `data/raw/firm_rosters/*` | yes -- scraped from the firms' own sites | **decides SAME only** (see below) |
| Act! `businessAddress` vs `advisor_branches.parquet` | yes | **recorded, never decisive** -- an address names an office, and two different people sit in one office |

Three deliberate refusals:

* **A disagreeing phone owner is not evidence.** The first version of the
  labeller called a row DIFFERENT when the roster put another name on its
  number. That produced 48 labels, including Act! "Ellen Takagi-Walsh" against
  SEC ELLEN MITSUE TAKAGI-WALSH -- plainly one person -- because 617 725 2000 is
  the number the RBC roster prints against Joel Slovin. Act! stores whatever
  number the desk had, usually the team or floor line, and a shared line names a
  colleague, not an impostor. Only phone *agreement* survives.
* **SAME is asserted only on agreement that does not need the nickname table.**
  Same word, truncation, initialism, or the SEC's own `used_first_name`. "Bob"
  against a filed ROBERT is left UNKNOWN even though it is almost certainly one
  man: a label that leans on `src/nicknames.py` would be measuring
  `src/nicknames.py`.
* **DIFFERENT requires no agreement even under the permissive table AND a
  different first letter.** Jay/Jeffrey, Trigg/Thomas and every other diminutive
  nobody has written down begin with the letter they stand for. Those rows are
  UNKNOWN.

Anything left over is UNKNOWN. **The UNKNOWN rate is 52.5% in the stratified
sample and 26.9% over the full 41,594 rows.** No label was guessed.

## The sample

Stratified over (today's gate agrees / disagrees) x (high / review) x (has email
/ no email); a flat random sample would be ~90% "agrees, high, has email".

| stratum | population | drawn | SAME | DIFFERENT | UNKNOWN |
| --- | ---: | ---: | ---: | ---: | ---: |
| agrees / high / email | 24,307 | 63 | 51 | 0 | 12 |
| agrees / high / no-email | 2,418 | 75 | 21 | 0 | 54 |
| agrees / review / email | 9,134 | 60 | 55 | 0 | 5 |
| agrees / review / no-email | 1,727 | 75 | 1 | 0 | 74 |
| disagrees / high / email | 1,071 | 65 | 13 | 44 | 8 |
| disagrees / high / no-email | 55 | 45 | 2 | 0 | 43 |
| disagrees / review / email | 2,722 | 60 | 3 | 43 | 14 |
| disagrees / review / no-email | 160 | 50 | 1 | 0 | 49 |

`draw` separates the three sources: **random** (fixed seed, the only rows from
which an unbiased rate can be estimated), **phone** (extra rows drawn from the
no-email cells that a roster phone can settle -- deliberately enriched, reported
separately), **anchor** (the eight hand-verified cases, never counted in a rate).

## The rules

| key | rule | what it may look at |
| --- | --- | --- |
| a | the gate **at the start of this session** -- `first_name` and `middle_name` as filed, unsplit, `same_person()` | filing |
| b | (a) + an initials rule (AJ = Alexander Joseph) | filing |
| c | (b) + email local-part corroboration: forgive a disagreement the firm-issued mailbox settles | filing + email |
| d | **proposal**: name fields split on whitespace, `used_first_name` included, initials | filing only |
| e | (d) + the email as a two-way witness: also demotes a row whose names agree but whose mailbox belongs to someone else | filing + email |
| f | the gate **now on disk** -- another session added `used_first_name` to (a) mid-way through this work, unsplit, no initials | filing |

`(d)` differs from `(f)` in two respects only: it splits a name field on
whitespace (a filing of "KEVIN MICHAEL" currently offers only `kevin`, because
`same_person()` keeps the first word of each token -- 39 rows are demoted for
that alone), and it accepts an initialism.

## Results

### Demotions over all 41,594 matched rows (27,851 high-tier before any gate)

| rule | high-tier demoted | % of high | all rows disagreeing |
| --- | ---: | ---: | ---: |
| a | 1,126 | 4.0% | 4,008 |
| b | 1,112 | 4.0% | 3,990 |
| c | 831 | 3.0% | 3,592 |
| d | **817** | 2.9% | 3,560 |
| e | 905 | 3.2% | 3,731 |
| f | 851 | 3.1% | 3,621 |

### Confusion matrices

Positive = "demote". TP = demoted and labelled DIFFERENT.

**Stratified sample, random draw only (n = 204: 85 DIFFERENT, 119 SAME)**

| rule | TP | FP | FN | TN | precision | recall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| a | 85 | 15 | 0 | 104 | 0.850 | 1.000 |
| b | 85 | 13 | 0 | 106 | 0.867 | 1.000 |
| c | 85 | 2 | 0 | 117 | 0.977 | 1.000 |
| d | 85 | 0 | 0 | 119 | **1.000** | 1.000 |
| e | 85 | 0 | 0 | 119 | **1.000** | 1.000 |
| f | 85 | 3 | 0 | 116 | 0.966 | 1.000 |

**All labelled rows including anchors and the phone-enriched draw (n = 234)**

| rule | TP | FP | FN | TN | precision | recall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| a | 87 | 19 | 0 | 128 | 0.821 | 1.000 |
| b | 87 | 16 | 0 | 131 | 0.845 | 1.000 |
| c | 87 | 3 | 0 | 144 | 0.967 | 1.000 |
| d | 87 | 0 | 0 | 147 | **1.000** | 1.000 |
| e | 87 | 0 | 0 | 147 | **1.000** | 1.000 |
| f | 87 | 4 | 0 | 143 | 0.956 | 1.000 |

**Phone-labelled rows only -- no email was used to label these (n = 28, all SAME)**

| rule | FP (false demotions) | accuracy |
| --- | ---: | ---: |
| a | 4 | 0.857 |
| b | 4 | 0.857 |
| c | 3 | 0.893 |
| d | **0** | **1.000** |
| e | **0** | **1.000** |
| f | 2 | 0.929 |

**Population-weighted from the random draw** (each labelled row weighted by
stratum population / rows drawn; assumes labelling is missing-at-random inside a
stratum, which is optimistic -- see caveats):

| rule | weighted TP | weighted FP | precision |
| --- | ---: | ---: | ---: |
| a | 2,700 | 317 | 0.895 |
| b | 2,700 | 281 | 0.906 |
| c | 2,700 | 2 | 0.999 |
| d | 2,700 | 0 | 1.000 |
| e | 2,700 | 0 | 1.000 |
| f | 2,700 | 64 | 0.977 |

**Census -- the same evidence applied to every matched row.** 30,417 of 41,594
settled (2,902 DIFFERENT, 27,515 SAME); 11,177 UNKNOWN. This is not a sample, so
it has no sampling error; it inherits the labelling rule's blind spots instead.
The weighted estimate above (317 false demotions for rule a) lands within 5% of
the census count (331), which is the main reason to trust the sample.

| rule | TP | FP | FN | TN | precision | recall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| a | 2,797 | 331 | 105 | 27,184 | 0.894 | 0.964 |
| b | 2,797 | 315 | 105 | 27,200 | 0.899 | 0.964 |
| c | 2,797 | 6 | 105 | 27,509 | 0.998 | 0.964 |
| d | 2,795 | **30** | 107 | 27,485 | 0.989 | 0.963 |
| e | 2,902 | 4 | 0 | 27,511 | 0.999 | 1.000 |
| f | 2,795 | 74 | 107 | 27,441 | 0.974 | 0.963 |

## The rows each rule gets wrong

### (a) 331 false demotions, 105 missed mismatches

301 of the 331 are fixed by (d), and **290 of those are rows where the SEC
itself files the name Act! uses**, in `used_first_name`:

```
5135640  Act! 'JP LaCour'       SEC JEAN PAUL LACOUR      (files also as JEAN-PAUL)
2840228  Act! 'Chip Messenger'  SEC FRANK EDWARD MESSENGER (files also as CHIP)
3126187  Act! 'Bo Gibbs'        SEC EDWARD BYRON GIBBS    (files also as BO)
1111963  Act! 'Jared Karstetter' SEC JERRY JAMES KARSTETTER (files also as JARED)
1060324  Act! 'Mike Lavery'     SEC KEVIN MICHAEL LAVERY  (files also as MIKE)
4731310  Act! 'Kianna Shin'     SEC Hyun Sun Shin         (files also as KIANNA)
6255991  Act! 'Lucy Yan'        SEC XIAOLU YAN            (files also as Lucy)
5092303  Act! 'Liza McCartney'  SEC ELIZAVETA MCCARTNEY   (files also as LIZA)
6532314  Act! 'CJ Dever'        SEC CHRISTOPHER JOE DEVER (files also as CJ)
4879324  Act! 'Mimi Foerster'   SEC MARY MARGARET FOERSTER (files also as MIMI)
2000723  Act! 'Nenad Tufekcic'  SEC NED TUFEKCIC          (files also as NENAD)
```

242 of the 331 are high-tier, i.e. sync was actually withdrawn from them.

### (b) 315 false demotions -- the initials rule fixes 16 and nothing else

### (c) 6 false demotions, 105 missed mismatches

All six are rows the email could not settle and the phone could:

```
2594389  Act! 'Ted Douglass'   SEC ALFRED EUGENE DOUGLASS (used TED)
4368040  Act! 'Buck Wiley'     SEC F.M. Wiley             (used BUCK)
4116388  Act! 'Breezy Adams'   SEC BREEANN J ADAMS
3093297  Act! 'Jay Parker'     SEC S JAY PARKER           (used STEVEN)
2261131  Act! 'Scott Thompson' SEC L SCOTT THOMPSON       (used LESTER)
1133693  Act! 'Dave Rogers'    SEC DAVID MICHAEL ROGERS
```

### (d) 30 false demotions, 107 missed mismatches

The 30 in full are in the census output (`--errors d`). **25 of them are rows
whose Act! name agrees with neither the SEC filing nor the row's own mailbox** --
that is, the name field is simply wrong, and demoting is arguably the right
outcome even though the row does belong to that advisor:

```
2580004  Act! 'Michael Bowles'  mailbox scott.bowles@lpl.com    SEC MITCHELL SCOTT BOWLES
1090984  Act! 'Josh Burns'      mailbox jeremiah.burns@...      SEC JEREMIAH STANIFORD BURNS
1852575  Act! 'Erin Stephen'    mailbox eric.stephen@rbc.com    SEC ERIC JAMES STEPHEN
1812171  Act! 'Al Elghandour'   mailbox ashraf.elghandour@ubs   SEC ASHRAF ELGHANDOUR
2446234  Act! 'Terry Swindell'  mailbox charles.t.swindell@...  SEC CHARLES TRIMBLE SWINDELL
```

The remaining five are mechanical and cheap to fix (listed under Follow-ups):

```
3026862  Act! 'Jroge Ordonez'          typo for Jorge
5301263  Act! 'Craid Solenzio'         typo for Craig
5502887  Act! 'Mr.Jefferson Bartley'   honorific with no space after the dot
4261961  Act! 'Wiggins II'             a suffix where the given name should be
 841868  Act! 'Dave Jancisin'          SEC DAVID -- Dave/David is missing from nicknames.py
```

### (e) 4 false demotions, 0 missed mismatches

```
7592063  Act! 'Ahmad Bahrami'   mailbox admad.bahrami@ml.com  (typo in the mailbox)
4116388  Act! 'Breezy Adams'    SEC BREEANN J ADAMS
6091981  Act! 'David Thompson'  mailbox dave.thompson@...     SEC DAVID EDWARD THOMPSON
1133693  Act! 'Dave Rogers'     mailbox dave.rogers@rbc.com   SEC DAVID MICHAEL ROGERS
```

All four were labelled by the **phone**, so they are genuine independent errors
and not an artefact of (e) reading the same field the label came from. Three of
the four are the Dave/David gap again.

### (f) 74 false demotions, 107 missed mismatches

44 more false demotions than (d) on identical recall, for the two reasons in the
rule table: no whitespace split, no initialism.

### Missed mismatches, all rules

Every rule except (e) misses the same ~105: a row whose Act! **name** matches the
filing while its **mailbox** belongs to a different person of that surname. No
name-only gate can see these.

```
2296027  Act! 'John Burton'      mailbox phil.burton@lpl.com      SEC JOHN KEVIN BURTON
5015149  Act! 'Michael Patterson' mailbox drew_patterson@ml.com   SEC Michael A Patterson
3160094  Act! 'Shajhan Sabir'    mailbox john.sabir@ml.com        SEC SHAJHAN SABIR
1902205  Act! 'Harry Buzzerd'    mailbox buddy.buzzerd@ubs.com    SEC HARRY WILLIAMS BUZZERD
2674521  Act! 'Armando Garofalo' mailbox gary.garofalo@lpl.com    SEC ARMANDO COSMO GAROFALO
```

33 of the 107 are high-tier, i.e. currently syncable.

## The anchors, and one that does not survive its own evidence

| CRD | Act! name | wanted | a | b | c | d | e | f |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 843291 | Marlyn Campbell | demote | demote | demote | **keep** | **keep** | **keep** | **keep** |
| 6426473 | Dave Harris | demote | demote | demote | demote | demote | demote | demote |
| 7601570 | Jason Main | demote | demote | demote | demote | demote | demote | demote |
| 2387916 | James Nowakowski | keep | keep | keep | keep | keep | keep | keep |
| 2052211 | Raymond Mones | keep | keep | keep | keep | keep | keep | keep |
| 1806841 | Brandt Haring | keep | keep | keep | keep | keep | keep | keep |
| 6838725 | AJ Gallego | keep | **demote** | keep | keep | keep | keep | keep |
| 1267466 | Hank Rottenberg | keep | **demote** | **demote** | keep | keep | keep | keep |

**CRD 843291 is not a mismatch, and two independent witnesses say so.** The
individual's own Form U4 in `IA_INDVL_Feed_07_20_2026.xml.zip` carries:

```xml
<Info firstNm="WAYNE" lastNm="CAMPBELL"/>
<OthrNms>
  <OthrNm firstNm="WAYNE"  lastNm="CAMPBELL"/>
  <OthrNm firstNm="MARLYN" midNm="WAYNE" lastNm="CAMPBELL"/>
</OthrNms>
```

Marlyn is the SEC's own record of a name this person filed, which is why
`used_first_name` holds MARLYN. The Act! record's email is
`wayne.campbell@lpl.com` -- the same person from the other direction. The
anchor's premise ("SEC WAYNE CAMPBELL, therefore a different human") reads only
the `first_name` column of `advisors.parquet`; the columns beside it disagree.
This is the one anchor no rule here satisfies, and the recommendation is to
retire it rather than to build a rule that fails the filing to satisfy it.

Note the sharp edge: the same `used_first_name` field that rescues Hank
Rottenberg (HERBERT, files also as HANK) and AJ Gallego is what keeps Marlyn
Campbell. There is no principled rule that trusts SEC other-names in the first
two cases and distrusts them in the third.

## Caveats -- read these before quoting a number

1. **Recall is not measured.** Every rule scores recall 1.000 on the sample
   because no labelled DIFFERENT row sits in an "agrees" stratum. That is a
   property of the evidence, not of the rules: the only witness that can prove a
   mismatch (a mailbox naming someone else) almost always comes with a name
   disagreement too. The census finds 105 missed mismatches for rules a-d, and
   that is a floor, not an estimate.
2. **Rules (c) and (e) read the field most labels came from.** 30,010 of the
   30,417 settled rows were settled by the email. (e)'s census recall of 1.000
   is therefore near-definitional and must not be quoted as an independent
   result. Its 4 false demotions, all phone-labelled, are independent; so is
   every number for (a), (b), (d) and (f), which never read the email.
3. **UNKNOWN is 26.9% of the population and 52.5% of the sample.** No-email rows
   are 10.5% of matched rows and are nearly unlabellable: 220 of 245 sampled came
   back UNKNOWN. Nothing here measures how a gate behaves on them.
4. **Labels are rule-applied, not eyeballed.** Each cites the evidence string
   that produced it in `gate_truth.csv`, so any label can be audited, but no
   human read all 234.
5. **`src/act_crosswalk.py` changed while this was being written** -- another
   session added `used_first_name` to the gate, moving it from 1,126 demotions
   (rule a) to 851 (rule f). Both are reported.
6. **A latent ordering bug was found and avoided here.** The initialism test is
   order-sensitive, so feeding it a Python `set` of filed names made the answer
   depend on the hash seed; two runs over identical data disagreed about two
   rows. `scripts/gate_eval.py` keeps filed names in an ordered list. Anyone
   implementing an initials rule in `src/` must do the same.

## Recommendation

**Adopt rule (d) in `src/act_crosswalk.py`: split the filed name fields on
whitespace, include `used_first_name`, and accept an initialism.**

Measured against today's gate (a), over all 41,594 matched rows:

| | a (session start) | f (now on disk) | **d (recommended)** |
| --- | ---: | ---: | ---: |
| high-tier demotions | 1,126 | 851 | **817** |
| false demotions (census) | 331 | 74 | **30** |
| missed mismatches (census) | 105 | 107 | 107 |
| precision (census) | 0.894 | 0.974 | **0.989** |
| precision (random sample, n=204) | 0.850 | 0.966 | **1.000** |
| anchors failed | 2 | 1 (843291) | 1 (843291) |

It costs two extra missed mismatches against (a) and removes 301 false
demotions, 242 of which are currently costing sync on correct matches. It reads
nothing but the SEC filing, so it adds no new dependency and cannot be
contaminated by Act! vouching for itself -- the failure mode this gate has hit
three times in three different files.

**Then run (e)'s email check as a flag, not as a demotion.** (e) is the only
rule that closes the 107 missed mismatches, and on settled evidence its 231
extra demotions are right 107 times, wrong twice and unsettled 122 times. But
its witness is a single field that can be stale or mis-keyed on the Act! side --
`admad.bahrami@ml.com` is a typo, not a different person -- and a demotion is
silent. Writing "the mailbox on this record names somebody else" into the
`demoted` column for those 231 rows puts them in front of a human without
withdrawing sync on a guess.

### Follow-ups worth doing, each with its measured cost

* **`Dave`/`David` is missing from `src/nicknames.py`.** It accounts for 5 of
  (d)'s 30 false demotions and 3 of (e)'s 4. One line in `EXTRA`.
* **Honorifics without a following space** (`Mr.Jefferson Bartley`,
  `Mr.Gary Smith`) defeat `normalise()`; the regex requires `\s+` after the dot.
  2 rows.
* **An Act! name that is a suffix or a bare initial** (`Wiggins II`, `G. Miller`)
  gives the gate nothing to compare. 3 rows, and they should be UNKNOWN-not-
  demoted rather than demoted.
* **`str(nan)` reaches the token set** in today's gate: a missing middle name
  contributes the literal word `nan`. Harmless so far, wrong on principle.
* **The 39 rows demoted purely because a name field was not split** are all
  correct matches, e.g. CRD 1060324 (KEVIN MICHAEL LAVERY, "Mike Lavery").
