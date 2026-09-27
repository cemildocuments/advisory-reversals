# Data attribution

The analysis software and original documentation are released under the MIT
license in `LICENSE`.

## GitHub Advisory Database

`data/blobs.jsonl.gz` preserves the original JSON content of selected GitHub
Advisory Database records, attributed to GitHub and its advisory contributors.
The source is <https://github.com/github/advisory-database>, frozen at commit
`7ccbff3744047c616177c0e7e39cfa6dd0623c0e` (27 September 2026).
The records are distributed under Creative Commons Attribution 4.0
International (CC BY 4.0). The source license is reproduced in
`LICENSES/GitHub-Advisory-Database-CC-BY-4.0.md` and the public license is at
<https://creativecommons.org/licenses/by/4.0/>.

The JSON records are losslessly stored as text in a compressed JSON-lines
container. The surrounding event index and derived tables organize historical
records for analysis. Transformations and selections are described in `README.md`.
Git blob hashes preserve the identity of each original record. This reuse is
independent research; attribution does not imply endorsement.

## Public registry metadata

`data/registry/` contains factual release-time projections from the public npm
registry (<https://registry.npmjs.org/>) and PyPI JSON API
(<https://pypi.org/pypi/>). Each record identifies its source URL, retrieval time,
HTTP status, and the SHA-256 digest of the retrieved response. The retained
projection is the package identity and version-to-release-time mapping used by
the experiment. npm timestamps are the registry's version timestamps; PyPI
timestamps are the earliest available distribution-upload timestamp per version.
These observations are attributed to the named registries and package publishers.
