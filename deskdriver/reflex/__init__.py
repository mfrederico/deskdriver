"""The reflex layer: a fast loop that reads the desk's frames and drives its keys, with a pluggable per-game policy.

    frames  -> vision (colour blobs, templates, motion, HUD bars) -> policy.step() -> input (held keys, clicks)

A policy is a small class with `step(view) -> None` that calls `view.keys.hold("space")` and friends; the runner
calls it at a fixed rate, logs what it did, and stops on time, on a stop file, or when the policy says so.
A language model is meant to sit above this (changing a policy's parameters every few seconds), never inside it.
"""
