{
  description = "A dummy API service";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        
        # Native Python (no external dependencies required)
        pythonEnv = pkgs.python3;

        # A script to run the dummy API
        run-script = pkgs.writeShellScriptBin "run-api" ''
          export PORT="''${PORT:-5000}"
          echo "Starting Dummy HTTP API on port $PORT..."
          exec ${pythonEnv}/bin/python ${./main.py}
        '';

      in
      {
        packages.default = run-script;

        apps.default = {
          type = "app";
          program = "${run-script}/bin/run-api";
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [ pythonEnv ];
        };

        # Heimdall Metadata
        heimdall-manifest = {
          commands = [ "run" ];
          healthcheck_url = "http://127.0.0.1:5000/";
        };
      }
    );
}
