# 50 CI/CD learning problems

For each question, start from its own broken branch and failing CI run. Repair the cause while preserving the project contract, public tests, and build and distribution checks. Each case is isolated; do not carry a code patch from one branch into another. The titles describe the observable problem, not the reference patch.

## Service dependency and runtime failures

1. **L01:** Move the current peer to major two.
2. **L02:** Distinguish transport signing from application signing.
3. **L03:** Reconcile the project dependency range, deployed lock, and stable nested request identity.
4. **L04:** Move the current peer to major two and respect a reporting peer's declared charset.
5. **L05:** Respect the declared charset and keep retry exhaustion as an error.
6. **L06:** Reconcile the dependency range and deployment lock while encoding list-valued query parameters.
7. **L07:** Keep a legacy peer on major one and distinguish transport from application signing.
8. **L08:** Repair transport signing and stable nested request identity.
9. **L09:** Move the current peer to major two, stabilize nested request identity, and handle an absolute Retry-After date.
10. **L10:** Keep a legacy peer on major one, respect its charset, and consume response bodies before closing.
11. **L11:** Follow a false-looking pagination cursor.
12. **L12:** Handle an HTTP error response that is still present.
13. **L13:** Handle a not-modified response without a JSON body and a present HTTP error response.
14. **L14:** Interpret an absolute Retry-After date and preserve the error on retry exhaustion.
15. **L15:** Respect a declared charset while handling a present HTTP error response.
16. **L16:** Consume a response before closing and handle mixed batch outcomes.
17. **L17:** Encode list-valued query parameters with stable nested request identity.
18. **L18:** Handle mixed batch outcomes and retry exhaustion.
19. **L19:** Stabilize nested request identity, preserve retry exhaustion, and handle mixed batch outcomes.
20. **L20:** Move the current peer to major two, follow a false-looking cursor, and handle a present HTTP error response.

## Packaging, build artifacts, and CI reports

21. **L21:** Make resources available after installation.
22. **L22:** Correct the wheel data manifest.
23. **L23:** Preserve installed resources and the source distribution data manifest.
24. **L24:** Correct both wheel and source distribution data manifests.
25. **L25:** Correct the wheel data manifest and reproducible archive metadata.
26. **L26:** Preserve installed resources and the wheel data manifest.
27. **L27:** Correct the wheel data manifest and detect ambiguous repeated archive members.
28. **L28:** Preserve installed resources and canonical Unicode report groups.
29. **L29:** Preserve installed resources, wheel data, and source distribution data.
30. **L30:** Correct wheel and source manifests while handling archive paths from another OS.
31. **L31:** Make archive metadata reproducible.
32. **L32:** Make framed manifests deterministic.
33. **L33:** Hash original artifact bytes and handle archive paths from another OS.
34. **L34:** Handle cross-OS archive paths and repeated archive members.
35. **L35:** Detect repeated archive members and make framed manifests deterministic.
36. **L36:** Parse quoted CSV report cells and mixed-unit durations.
37. **L37:** Canonicalize Unicode report groups and weight them correctly.
38. **L38:** Order releases and prereleases correctly while producing deterministic framed manifests.
39. **L39:** Weight report groups, order releases, and produce deterministic framed manifests.
40. **L40:** Produce deterministic framed manifests and reproducible archives while hashing original bytes.

## Release and deployment decisions

41. **L41:** Select artifacts from the current build.
42. **L42:** Block deployment when tests fail.
43. **L43:** Include the lockfile and matrix identity in the dependency cache key.
44. **L44:** Keep matrix artifact upload and download names distinct and matched.
45. **L45:** Require a successful build check for packaging changes.
46. **L46:** Propagate failed application readiness probes to the CI job.
47. **L47:** Ensure the release tag matches the built source.
48. **L48:** Use the same dependency lock for tests and the published build.
49. **L49:** Bind deployment to the tested source SHA and built artifact digest.
50. **L50:** Report failed rollout and restore the last healthy artifact.
