"""Diamond — narrated design & code review engine.

A reviewer opens the product under development, talks while pointing with the
mouse, and Diamond turns that session into evidence-backed findings that are
traced to source code and handed to a fixing agent.

Layers (each package is designed to be extracted as a standalone skill):

* ``capture``   — browser session, pointer/DOM/perf telemetry, microphone
* ``speech``    — speech-to-text with word timestamps + Persian UI lexicon
* ``fusion``    — aligns what was *said* with what was *pointed at*
* ``analyzers`` — deterministic checks (consistency, perf, a11y, RTL, …)
* ``locate``    — DOM element → source file:line
* ``report``    — Persian report, JSON, PQE export, regression tests
* ``fix``       — fix briefs + model routing for the fixing agent
"""

__version__ = "0.1.2"
