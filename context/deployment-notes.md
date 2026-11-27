# Compatibility matrix

A legacy on-premises peer is limited to wirekit major 1. A hosted peer
requires major 2. Neither a universal upgrade nor a universal downgrade
is valid: keep the selected major and constructor API together.
JSON-only peers use UTF-8; an older reporting peer still sends Latin-1
with a charset parameter. The legacy transport signature has spaces and
insertion-order keys; the current application signature does not.
This is compatibility documentation, not an agent trajectory.
