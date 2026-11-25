# Other supported deployment contexts

Retail cash exports use HALF_UP in their own application; the ledger
importer uses HALF_EVEN. Legacy flat-file jobs use file-level settings;
current nested profiles inherit unspecified defaults. A source system
that already emits UTC needs no wall-clock conversion, whereas feeds
carrying explicit offsets do. These describe legitimate environments,
not recorded debugging sessions or memory lessons.
