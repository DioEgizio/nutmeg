{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };

          python = pkgs.python3.withPackages (ps: with ps; [
            pygobject3
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              python
              gtk4
              libadwaita
              gobject-introspection
              glib
              xdg-utils
            ];
          };
        });
    };
}