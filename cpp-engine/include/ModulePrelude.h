// ModulePrelude.h — force-included before every translation unit.
// Provides all types/headers that STAR-style module headers depend on.
// Module .h files must NOT contain any #include (only #pragma once).
#pragma once

#include <vector>
#include <string>
#include <cmath>
#include <cstring>
#include <limits>
#include <algorithm>
#include <ostream>
#include <istream>

#include "signal_vector.h"
#include "star_vector.h"
#include "module.h"
#include "data/data_panel.h"
