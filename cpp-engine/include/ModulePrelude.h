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
#include <Eigen/Dense>   // linear algebra for modules (header-only, fetched by CMake)
#include "signal_vector.h"
#include "star_vector.h"
#include "module.h"
#include "data/data_panel.h"
#include "simd/m_decay_avx2.h"

using std::isfinite;
using std::isnan;
