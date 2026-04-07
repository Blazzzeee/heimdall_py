{
  description = "A dummy background worker service";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        pythonEnv = pkgs.python3.withPackages (ps: with ps; [
          # Add worker dependencies here if needed
        ]);

        run-script = pkgs.writeShellScriptBin "run-worker" ''
          echo "Starting Dummy Worker Service..."
          exec ${pythonEnv}/bin/python ${./main.py}
        '';

      in
      {
        packages.default = run-script;

        apps.default = {
          type = "app";
          program = "${run-script}/bin/run-worker";
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [ pythonEnv ];
        };
      }
    );
}
