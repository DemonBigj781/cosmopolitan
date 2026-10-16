#include <cstdio>
#include <memory>
#include <string>
#include <vector>

int main() {
  std::vector<int> values{6, 7};
  auto result = std::make_unique<int>(values[0] * values[1]);
  std::string message = "C++ SMOKE: ";
  try {
    throw *result;
  } catch (int value) {
    std::printf("%s%d\n", message.c_str(), value);
    return value == 42 ? 0 : 1;
  }
}
