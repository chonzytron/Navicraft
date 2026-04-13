.PHONY: build package clean

# Build the WASM module using TinyGo
build:
	tinygo build -o plugin.wasm -target wasip1 .

# Package as .ndp file for Navidrome
package: build
	zip -j navicraft.ndp manifest.json plugin.wasm

# Clean build artifacts
clean:
	rm -f plugin.wasm navicraft.ndp
