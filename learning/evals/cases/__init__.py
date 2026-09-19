from .basic_func.basicFunction import CASES as BASIC
from .basic_func.noPersistentCwd import CASES as NO_CWD
from .basic_func.truncation import CASES as TRUNC
from .basic_func.budgetExhaustion import CASES as BUDGET
from .basic_func.resume import CASES as RESUME

from .tools.read_specific_line import CASES as READLINE

#Stage 1
ALL_CASES = [*BASIC, *NO_CWD, *TRUNC, *BUDGET, *RESUME]

# Stage 2 - Tools
# ALL_CASES = [*READLINE]

