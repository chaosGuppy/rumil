"""Registries mapping variant names to call classes.

Only the assess call has multiple variants ("default" vs "big"); all other
call types use a single concrete class.
"""

from rumil.calls.assess import AssessCall, BigAssessCall

ASSESS_CALL_CLASSES: dict[str, type[AssessCall]] = {
    "default": AssessCall,
    "big": BigAssessCall,
}
