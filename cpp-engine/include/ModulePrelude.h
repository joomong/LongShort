// ModulePrelude.h — force-included before every translation unit.
// Provides all types/headers that STAR-style module headers depend on.
// Module headers may use these shared types without repeating their includes.
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
#include "simulation_data.h"
#include "module_registration.h"

using std::isfinite;
using std::isnan;
