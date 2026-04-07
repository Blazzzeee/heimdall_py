{
  description = "A minimal NodeJS service for Heimdall";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        nodejs = pkgs.nodejs;

        run-script = pkgs.writeShellScriptBin "run-node" ''
          export PORT="''${PORT:-3000}"
          echo "Starting NodeJS Service on port $PORT..."
          exec ${nodejs}/bin/node ${./index.js}
        '';

      in
      {
        packages.default = run-script;

        apps.default = {
          type = "app";
          program = "${run-script}/bin/run-node";
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [ nodejs ];
        };

        # Heimdall Metadata
        heimdall-manifest = {
          commands = [ "run" ];
          healthcheck_url = "http://127.0.0.1:3000/";
        };
      }
    );
}
