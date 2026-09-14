from .basicFunction import CASES as BASIC
from .noPersistentCwd import CASES as NO_CWD
from .truncation import CASES as TRUNC
from .budgetExhaustion import CASES as BUDGET

from .read_specific_line import CASES as READLINE

#Stage 1
# ALL_CASES = [*BASIC, *NO_CWD, *TRUNC, *BUDGET]

#Stage 2
ALL_CASES = [*READLINE]