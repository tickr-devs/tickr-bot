"""Runtime around the bot process: preflight, alerts, signal handling, launchd supervision.

Nothing here trades. These modules decide whether the bot may start, tell the owner when something
needs attention, and turn SIGTERM into a "stop entries" request the main loop acts on.
"""
