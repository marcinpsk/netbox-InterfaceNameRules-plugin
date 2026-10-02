# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- version list -->

## v1.8.1 (2026-10-02)

### Bug Fixes

- Require hashes for locked CI dependency installs
  ([`b23cedb`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b23cedb4192c7ead4094668a5941ba656af7dd46))

- Use locked CI dependencies and separate save warnings
  ([`cf5516d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/cf5516dc83427ae4bdd74d31922f377c1885b51f))

### Testing

- Remove command-shape assertions from CI checks
  ([`f4bbcbf`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/f4bbcbffb3af5ce8682e52acb5d5c95095bbff50))


## v1.8.0 (2026-10-01)

### Bug Fixes

- **models**: Check the write alias on every rule save
  ([`e7e5b58`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/e7e5b58032df5dfeeedf82779d516afe435b0a9e))

- **models**: Leave a replayed rule save to netbox-branching
  ([`660895e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/660895eb38290ff5fe5db42e96344e1cf98aedba))

### Build System

- Make the coverage gate exact and run it only on the combined data
  ([`86f16f1`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/86f16f17629e573b1df7a1958d82fb41def4ff40))

### Chores

- **deps**: Bump the github-actions group with 3 updates
  ([`abe1ba8`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/abe1ba8cf69412c2268e7458d55f42c08086b53a))

- **deps**: Bump urllib3
  ([`f2b1473`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/f2b14730e407e3244a856aae256bd1a0e4290fc1))

- **deps**: Bump virtualenv
  ([`a28c47b`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/a28c47ba9443e5a824a9292ed1358ec17db2b83d))

- **deps-dev**: Bump build from 1.4.0 to 1.6.1
  ([`3ed011f`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/3ed011fa134fd01dbe3276f21d2f4de657cb4a4b))

- **deps-dev**: Bump python-semantic-release from 10.6.2 to 10.7.0
  ([`234fa8c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/234fa8c46578603a4ed2ad99a45e3e5bed7ea169))

- **deps-dev**: Bump ruff from 0.16.0 to 0.16.8
  ([`300409c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/300409c1acd84ccc874fb979df598ea42c06298d))

### Continuous Integration

- Enforce the coverage gate on the combined data of two legs
  ([`d4b21f6`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/d4b21f651fad24ac4901202595db7248ad231063))

### Documentation

- Name the transaction and branching modules in the agent instructions
  ([`a22ef3d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/a22ef3d7938fcafc2004a6f0a23fbfdec4bab246))

- State the alias and the commit that the rename triggers use
  ([`7443728`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/74437284f748f1c03b3aed50b39c328973a74c8b))

- **configuration**: Describe netbox-branching support and its limits
  ([`148fa23`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/148fa2379b3288853bd5cfc3912193fcfa961e3d))

- **configuration**: Put a script install in a transaction on the interface write alias
  ([`05535b8`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/05535b89744c150a01cd2a9a06db97c607a5323d))

### Features

- **api**: Refuse a background rule request in a netbox-branching branch
  ([`2b972b7`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2b972b755267a2f37cdddc78b43732f019bf73b1))

- **jobs**: Run Apply Rules and Convert jobs in the branch they were enqueued from
  ([`5598d35`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5598d350a38cc8c603f317cddfcb5fec6fd4022e))

- **triggers**: Do nothing while netbox-branching merges, reverts or syncs
  ([`d98ce48`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/d98ce48d512382ab7d2496e6788f1d0992bda962))

- **triggers**: Run the rename triggers on the connection of the save
  ([`2e46e98`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2e46e98e5104466b0471ddfd61adcbac0708a311))

### Refactoring

- **branching**: Ask in one module whether netbox-branching is installed
  ([`cb9f40b`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/cb9f40b86375033766e0bd7a44d3a027eba1c3f0))

- **tests**: Define the shared test fixtures once, outside the test modules
  ([`c12a9e4`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/c12a9e44d855c40612e263587c3a0f796024cae1))

- **tests**: Share the bay and interface helpers of the branch cases
  ([`620407e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/620407e2ec6e78abb8a9c9af7f3dde1c638a4a02))

- **tests**: Split the channel rows of the branch write tests
  ([`8cc2c2f`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8cc2c2f286baf3f8031a13cceb7d98074a6bccc1))

### Testing

- **boundaries**: Read the whole test package, subpackages and __init__.py included
  ([`2af2bb0`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2af2bb0d118674167bb92ca748571f9bfb7f9c7e))

- **boundaries**: Refuse a wildcard import from the test package
  ([`6414a1c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/6414a1c701e580c133626f228ca7b4092a3159af))

- **boundaries**: Refuse an import of a test module inside the test package
  ([`a27508d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/a27508dcf1520d99767360eab643807ac81dd644))

- **branch**: Check the ObjectChange of every rename in a branch install
  ([`6ff7c58`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/6ff7c581d3d60658b89b57625eda968871a29c2d))

- **branch**: Move a module in a branch
  ([`d436135`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/d436135591da7fa5fb1b5120b205d2d423294f35))

- **branching**: Count every replay reference with its receiver and scope
  ([`12c10b4`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/12c10b467d2b55e146d163890a6675f0c1881fa1))

- **branching**: Fingerprint each reviewed scope of netbox-branching
  ([`22a7430`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/22a7430534151655cc9e6f8534203336a6200239))

- **branching**: Pin each replay call site of netbox-branching
  ([`204eb91`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/204eb91137cba24160c6b2c56e79b647761054ca))

- **branching**: Pin the reviewed netbox-branching release
  ([`1e66e42`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/1e66e42b948d707c5fc6d96410bbf5b9b0855072))

- **guard**: Count a None alias as no alias in a named exception
  ([`adfdd59`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/adfdd59cfcbf5170b3ab3a7849a4db14f6f30dd6))


## v1.7.0 (2026-09-30)

### Bug Fixes

- **change-log**: Count a block as committed only after its atomic exit
  ([`e216f54`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/e216f54eff4a1b9bf90ba0c5e2512f9ef7a5926f))

- **change-log**: Keep the NetBox events of an atomic block only when it commits
  ([`2a9c014`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2a9c014fd7efa5209f227d790cef60f789c84ac6))

- **change-log**: Point each enclosing event at the saved row after a rollback
  ([`47f1406`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/47f14063bf9b2c1de1bedb630cf4e71970085c0a))

- **change-log**: Record the before-state of each row the plugin changes
  ([`4053d27`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/4053d2712935eac49ab85fd2a5e35253a2f365a7))

- **change-log**: Reload an enclosing event's object after its block rolls back
  ([`fd63f6e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/fd63f6eced5c37bb645a35567bf0c0cc55e047aa))

- **change-log**: Tell a failed commit callback apart from a rollback
  ([`5521641`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5521641117e1a308e652377fab2ad63bfceda41b))

- **claims**: Build a breakout family only on a name one template claims
  ([`8d138cc`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8d138cc1b7ec2db12acc29a0e64dc93a4fda4498))

- **claims**: Build a breakout family only on a name one template claims
  ([`80b682a`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/80b682a7935f33b3e5bf6d01f21e52ad44dbd304))

- **claims**: Keep a selected interface when a family takes its name
  ([`77796fa`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/77796fa79b716d098dadc0ca8a8001054e61da71))

- **claims**: Keep a selected interface when a family takes its name
  ([`443672e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/443672e379fa1126e8ccefb89e8d7523adc8f257))

- **claims**: Let only a family that builds take another interface's name
  ([`b3b929c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b3b929cce702c8677756c5ede111343ac2f29b42))

- **claims**: Let only a family that builds take another interface's name
  ([`394fd6f`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/394fd6f03aa8e2470e1e0d3b7479dc152e9aacb7))

- **claims**: Read the claim of a module type without templates like any other
  ([`8f4a633`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8f4a63303eef70b0dfde4d9ccaa305e377eada20))

- **claims**: Read the claim of a module type without templates like any other
  ([`ec18909`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ec189096bfcb77b828b1c191d4a45184ec38d778))

- **claims**: Rename a refused raw name under a rule without {base}
  ([`158c4ff`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/158c4ff2a1b5280f29ff0dac282b29297b276710))

- **claims**: Rename a refused raw name under a rule without {base}
  ([`6b52b66`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/6b52b66c606f40cd6dece4a361f18b5e7cd8521f))

- **claims**: Report a selected interface beside channelized families
  ([`8a52fee`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8a52fee1d17d9fb69d30fb19f377e847c1a56202))

- **claims**: Report each interface a forced breakout reapply cannot claim
  ([`7a45df0`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/7a45df0c0a9ac0666deba5b7b60c6f0dee0f2f1f))

- **claims**: Report each interface a forced breakout reapply cannot claim
  ([`aec1d63`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/aec1d63aa5e485eac515ce8936599f4e29b75718))

- **engine**: Lock and re-read the rule before it gets the deprecated tag
  ([`fd4fc5d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/fd4fc5d80f6a248c18108ab4555854ce9ee94cea))

- **engine**: Offer no family in the preview whose names are in use
  ([`6047005`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/604700568190764cd58394738af49bb6aa95c1ff))

- **engine**: Offer no family in the preview whose names are in use
  ([`9b68afd`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/9b68afd1cb573f03a526014fcbc683864d8b1965))

- **engine**: Report only the rows a creation plan acts on
  ([`af37a29`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/af37a296cdb5464fbf193c61cdfd9ef2502cc6a8))

- **engine**: Report only the rows a creation plan acts on
  ([`8ffc5ff`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8ffc5ff8c0f2c9645521e08b1aa29e6766ace47d))

- **family**: Let a flat family keep only the rows the claim gave it
  ([`d890e82`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/d890e822476bebb7caf73506666332f0d93910cc))

- **family**: Let a flat family keep only the rows the claim gave it
  ([`8157623`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8157623f3155a3d633317ca8cac90af2ebd207f6))

- **family**: Report the rows a refused flat family would adopt
  ([`97ca250`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/97ca250ff18047548bf46e116c455f69986143e2))

- **family**: Report the rows a refused flat family would adopt
  ([`005a34e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/005a34e13b174c28e15e1103b19879616dbbdaa3))

- **jobs**: Give both jobs a logger on NetBox 4.3
  ([`5f5131e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5f5131e11bf1ded29bc8e605c39e3d15eed88183))

- **jobs**: Give the job request every attribute that event rules copy
  ([`40c4a7d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/40c4a7d92be6e59f707667f85112ec3c89581c68))

- **jobs**: Record the changes of the Apply Rules and conversion jobs
  ([`68798bd`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/68798bd078029d4e33a7e7b1c7191fe990566ed2))

- **performance**: Name the scenarios without a baseline as not assessed
  ([`19ab880`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/19ab8809e1f52ae8b9f13862a39c3058f5c23dc3))

- **rename-triggers**: Append a plan runner per trigger instead of moving the plan callback
  ([`d8ee880`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/d8ee88011d530535967cce5dd90e501548182ea0))

- **rename-triggers**: Build a flat family for a module installed and moved or bay-edited in one
  transaction
  ([`a3a9666`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/a3a966617ca3c0c68df1013e36e4de8b216fc19c))

- **rename-triggers**: Detect NetBox module moves with one probe that the NetBox 4.7 leg asserts
  ([`cdf57e9`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/cdf57e9538b815969c6525494d702ee23d66f5a7))

- **rename-triggers**: Name a move with NetBox's move resolver and take the latest naming point
  ([`c541c1c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/c541c1c2029eb8b5436aa5bdb0cd12b1aa131d4e))

- **rename-triggers**: Name an installed module from its naming when its bay chain changed back
  ([`55c2011`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/55c20118333898f61fad4518616ebe471cafb814))

- **rename-triggers**: Reapply a module once when its device also changes chassis position
  ([`72fed0c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/72fed0c0080173d863c4410dc41c79a81a1209a3))

- **rename-triggers**: Reapply a module that moves out and back with the names that its moves gave
  ([`51ce500`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/51ce5001b42574b30c2b3045fd71b6c5f03da8a2))

- **rename-triggers**: Reapply an installed module as an install that also knows the position of its
  install
  ([`41fb319`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/41fb319f0a6dd49f135e2342c33ab0e69367ecd5))

- **rename-triggers**: Reapply each module once per transaction from one reapply plan
  ([`c3479ca`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/c3479ca0053cc3cae05b2f14514a99eabb7e1c49))

- **rename-triggers**: Recognise a module installed after a chassis change from the position at its
  install
  ([`66d995c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/66d995c9cac60c60c2de27fb14c75e8527d79107))

- **rename-triggers**: Recognise an installed module at the position of its install when no trigger
  read its naming
  ([`b8aedea`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b8aedea8920a2f224766faa874684fc6f2e31b8d))

- **rename-triggers**: Recognise the raw names of every naming point of a module in the transaction
  ([`b5c3c13`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b5c3c13692822e503851d4370c2fafa2772aff74))

- **rename-triggers**: Recognise the raw names that NetBox gives at a move before a chassis change
  ([`25e6145`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/25e614533dab869a45dbcc987ba731bb2095290c))

- **rename-triggers**: Rename a nested module from its earliest naming when two of its bays change
  ([`f1003f9`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/f1003f99deb74341af0a5ec5fa4fd41b80a4add4))

- **rename-triggers**: Report a failed naming at install on its module, like a failed reapply
  ([`1210db0`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/1210db055207b00c16683e1a38f4499ce44654ae))

- **rename-triggers**: Report a failed rule comparison as a failure of its module
  ([`660da4c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/660da4c137a8c2ea99c316045004b959bc62f039))

- **rename-triggers**: Resolve raw template names at the chassis position before a change
  ([`2dd302c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2dd302cfaf613ebb3afa182e47563d709db759c2))

- **template-names**: Mark the chassis-position tokens with text no template name can hold
  ([`00c83ec`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/00c83ec16908d991697a0379315c7d21cf97ac53))

- **template-names**: Resolve a template name at another chassis position through NetBox's own
  resolver
  ([`a2b500a`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/a2b500ade238f730d0564fcb66be68ae8425d259))

- **template-names**: Split a template name at every chassis-position token that NetBox resolves
  ([`e92f1e6`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/e92f1e6f7d3eaba29fbe5cafbed037eb058f5a1f))

- **views**: Lock the rule before the enable toggle takes its snapshot
  ([`4a68dd6`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/4a68dd6165f85531623b0040ac5341afe4841e3a))

### Chores

- **coverage**: Measure the channelized planner on every release
  ([`656fd48`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/656fd489ed0988406012aa098df7add2ed89ef6e))

### Continuous Integration

- Run the suite against a provisioned netbox-branching branch
  ([`152a808`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/152a80801b0daa539673166bddc78b00d602a984))

### Documentation

- Define the naming point and state the install rule of one transaction once
  ([`0e722d4`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/0e722d408a216a00b8c05c6c9524609c88f16a8e))

- Describe how a module type change renames the modules nested in it
  ([`408b710`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/408b7102ede6c053c353c6e2e382926d68913dd5))

- Describe the bay edit rename trigger and drop its limit before a move
  ([`186c8cc`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/186c8ccf33b2e9294d1416036e274dbcd35b6538))

- Describe the plan runners and qualify the once-per-transaction wording
  ([`93d2105`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/93d21054fb5be346343f09dcb9a79ed46de35ddb))

- Describe the reapply plan of a transaction and its journal owner
  ([`7edd793`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/7edd793f01f46f681096fd03daf299599de700fc))

- Drop the limits of a chassis-position change with a module change in one transaction
  ([`1e75770`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/1e75770d75bdd4b541f805d157210e7c0edef9c8))

- List the limit of a name that NetBox gives at a move before a chassis change
  ([`482df4c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/482df4c58c6b07639dd2f71c123641dc51628ffc))

- Qualify the bay name trigger in the feature lists
  ([`04727cf`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/04727cf1e20b7e59c4113ec02d9cad3c76744be7))

- Record the shared earliest naming and the install case of the bay edit trigger
  ([`de05dad`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/de05dad2c988b3a436deed4d544f0c6d2c9a7200))

- Split the Signal-driven feature line into short sentences
  ([`7ceaba8`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/7ceaba8664bf293eeae416f7bb4539510948d66d))

- Split the type-change sentences and record the scope check timing
  ([`a4b79e8`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/a4b79e8b3ba57f4ce21f35428f72ea30d3126166))

- State that a chassis-position change and a module trigger can reapply one module twice
  ([`b366f7d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b366f7d101e0b363e724d9a5dc734c8abcdcf4d5))

- State which chassis position a naming takes and which modules a device leaves out
  ([`4b2bd38`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/4b2bd3803bfff6311d99d8fc390ba13410080597))

- **adr**: Run plugin writes and rename triggers on the write alias
  ([`fa9fe52`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/fa9fe5202e5e7f595c403a1999b9f36099502eae))

- **adr**: State one claim resolution order for every path
  ([`5cdf874`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5cdf8743c20927d0fbbe60185e143f4bc418de25))

- **adr**: State one claim resolution order for every path
  ([`62b95dd`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/62b95ddef30a29f3bf15cae48c36fd6292f071ea))

- **change-log**: Describe the change-log records of plugin writes and jobs
  ([`5dd5ea0`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5dd5ea01a684fb6e90975d6115fb4e18b324e860))

- **change-log**: Say that a family the plugin cannot finish sends no event
  ([`431b847`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/431b84709c2c04d2709482bb6a09ed4f5c5f0fde))

- **change-log**: Scope the no-event sentence to changes the plugin rolls back
  ([`9777178`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/9777178ee88504dc217209c7ed992d62f216409a))

- **design**: Record the netbox-branching design ratified in six review rounds
  ([`53088ed`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/53088ed1a0123787af422e3f49400aff1e4835b1))

- **design**: Separate the executed review checks from the unrun netbox-branching claims
  ([`dbbee2a`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/dbbee2a92d6536f1c103b0b40bc261ab0ebe6310))

- **performance**: Keep the test database in the displayed measurement command
  ([`3784ce3`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/3784ce30c8463156d611394e52c05c2ca263c2ad))

- **performance**: Measure the module move trigger at its merge commit
  ([`914bc58`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/914bc58d36950072b87edbf0a5e03599d73f57f6))

### Features

- **branching**: Refuse an unsupported netbox-branching version at startup
  ([`ddc2ab0`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ddc2ab045e4c521beef59299e314ffb4053bfbf8))

- **claims**: Resolve every name form in one claim pass on every path
  ([`8b41d39`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8b41d394283bcbd76c5360683f4549c5837a6410))

- **claims**: Resolve every name form in one claim pass on every path
  ([`4ba09aa`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/4ba09aaaf8f67818b2eeb29ba92397b626acd65d))

- **rename-triggers**: Recognise module names after a chassis change earlier in the transaction
  ([`6a91c3b`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/6a91c3b8770197b6f6b8ea4b7021eaacad52580e))

- **rename-triggers**: Rename nested modules after their parent module's type changes
  ([`c10af98`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/c10af98bd648d4b4641d24815d69be3eb09974e4))

- **rename-triggers**: Rename the modules in a bay after its position or name changes
  ([`4ce0673`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/4ce06738dd5fb335a0c8b6828c1f8c17dd464b33))

### Refactoring

- Reduce the complexity that SonarCloud reports on develop
  ([`a3931be`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/a3931be1e01bf9bbdccf722a568a55b71edb5e41))

- **ci**: Move the mapping check out of _check_model
  ([`9543961`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/9543961e8254042b6bbc3874087c1a86d7844bd0))

- **claims**: Apply the run scope in the family package
  ([`ca1f2f0`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ca1f2f0f00e26292f2c12b94f4e0fcafced94996))

- **claims**: Apply the run scope in the family package
  ([`10275ca`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/10275ca74f7c8e78f807643291f7fa919097d20d))

- **engine**: Derive the variables of a naming from its bay chain and chassis position
  ([`7eca25e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/7eca25edeb2e7391f36c352f24de36d07cb471d8))

- **engine**: Make the rule comparison a ModuleNaming method
  ([`61fb7b5`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/61fb7b595aeec5e554cbf975df86b5f6706bd77a))

- **family**: Decide flat expansion from the rows the planner holds
  ([`51e02d7`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/51e02d75d5ab8cbde5b3179927d8f9b8bf6c9086))

- **family**: Decide flat expansion from the rows the planner holds
  ([`ae9d838`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ae9d8381d405fb8f1165b5eed7df25680f078c92))

- **family**: Move the kept-module plan out of plan_module_families
  ([`16e6fa6`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/16e6fa6e703078eb97113b2fbf19496165d23b19))

- **family**: Remove the unused live_members of the plan classes
  ([`0f252b0`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/0f252b0787385dfdf13564b6a453d929282f56c6))

- **family**: Remove the unused live_members of the plan classes
  ([`24df1bb`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/24df1bb0d55bf325a6e77134b9ddb5bc054337b3))

- **jobs**: Move the rule lookup and the request into RuleJobRunner
  ([`305304e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/305304ed968bdea9dadad4df7d885d3ea038962f))

- **naming**: Define the chassis position of the variables once
  ([`bd34058`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/bd34058b05dc109836adff7910bcabdb82d69d87))

- **naming**: Read the chassis membership of a device directly
  ([`f5746ca`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/f5746ca4bcd2c145e6c5814dbb8027cfca4407a0))

- **rename-triggers**: Define a module type change once on ModuleState
  ([`e34a7b4`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/e34a7b4ea4d29e39bdea5430873e4390116d7ede))

- **rename-triggers**: Hold a bay's previous state in a frozen dataclass
  ([`008b77f`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/008b77f872e23a1691a06a99395a057156372e5e))

- **rename-triggers**: Name the fields of a naming read and type the naming of a naming point
  ([`4dde978`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/4dde978a1dc4f756d5b67e67130a47a03e7ba678))

- **rename-triggers**: Split the module reapply into option, owner and run helpers
  ([`c430512`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/c430512da3fcb9d3091cffc24086fbf562dd3cbd))

- **rename-triggers**: Split the triggers of a plan once
  ([`2113b30`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2113b30233824f4865e9fdbe7f5be0aa9d8d54df))

- **rule-selection**: Read enabled module rules through one queryset
  ([`2060ab1`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2060ab1e89b5a75cee1d9702fdb603039d0f25b6))

- **tests**: Read module imports through one shared helper
  ([`39d0b6c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/39d0b6cca5d20954576e648bd9c16bda2c781f9b))

- **views**: Build the preview variables in a helper
  ([`8fc2f62`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8fc2f629ee3d3f63a7319eee0d4ebf323aaea234))

### Testing

- **branching**: Provision a real branch in tests
  ([`113690a`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/113690a7885596500730cf65184afe9daac8741f))

- **change-log**: Cover the delete skip and gate the fail-closed test to NetBox 4.5+
  ([`76a34a1`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/76a34a18dfd8d4bdad3cf2e277692b64de2ce4fe))

- **change-log**: Exempt an M2M change after a create only in the same request
  ([`b61110b`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b61110b59dc5299a6b4bfcbeab3cdbc4aba7f3f3))

- **change-log**: Fail a test when plugin code writes a row without a snapshot
  ([`b11cc1d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b11cc1da5dc5b58cf9ec1bdc1963937a004382de))

- **change-log**: Find the guard's calling frame without a fixed stack depth
  ([`12bdf74`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/12bdf743b64d7304b17d91cf19a60f72cee98a46))

- **claims**: Check each leftover beside a channelized family on every run
  ([`8fd9d86`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8fd9d8610f5465e8a353752f92c328689301c810))

- **claims**: Check the plans of every path on flat layouts
  ([`771a85c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/771a85c9aee1ad66c1d0bfc1b82828f5ada0b51b))

- **claims**: Check the plans of every path on flat layouts
  ([`581fe7f`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/581fe7fac1775326b249df629507d60a0f1b033f))

- **families**: Run the flat-expansion tests on NetBox releases before 4.7
  ([`3da91d8`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/3da91d8951c344b5e247fd3f12e2e0a3fffa631a))

- **jobs**: Share the unrunnable rule and the job log runner
  ([`09f6a1a`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/09f6a1af8671792aa669946217fbf950f398b2c7))

- **module-boundaries**: Key the bulk-write permits by call source
  ([`7a6be11`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/7a6be114834c313847465dedcb5377a34d99c95f))

- **module-boundaries**: Refuse an atomic block outside the transactions module
  ([`dd4872e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/dd4872e33ed05bac8810f4cb78c252b1adfd47b6))

- **module-boundaries**: Refuse bulk writes of change-logged rows
  ([`e97e77c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/e97e77c71c54f8c11751193d2d4e242f76f631b1))

- **performance**: Build nested move modules only for the nested scenario
  ([`448d639`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/448d639be249aadd69ca70356afa39a8fb3002ba))

- **performance**: Mark the direct-callback move trigger as moved
  ([`2103e81`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2103e814365c8fb109d816edab46935cbd858bd5))

- **rename-triggers**: Add moves back to the enumerated sequences of module and chassis changes
  ([`91b371d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/91b371d2534d066a56939c2314ec891b4230907a))

- **rename-triggers**: Assert that a bay edit in a rolled-back savepoint schedules no reapply
  ([`b9b1a3a`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b9b1a3a29c99bc5c264754812c3d34c39e4c2f8a))

- **rename-triggers**: Check every short sequence of module and chassis changes against the final
  state
  ([`a6191fc`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/a6191fcb655dd9259caa0dd46dc2bc73b3e20277))

- **rename-triggers**: Count only the reapply's queries in the chassis-wide reapply cost test
  ([`685f128`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/685f128aadaf8e0d3b459f3727ed8b7f513e1d15))

- **rename-triggers**: Cover a new bay saved with an id that has no row yet
  ([`f3a387e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/f3a387ed62c9eb6d5c071512ff1eede0e2a43ace))

- **rename-triggers**: Cover installs, joins and leaves beside a module change in both orders
  ([`5357d65`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5357d65bbc45ec93429074a3fcc423a29b4619db))

- **rename-triggers**: Guard a bay edit and a chassis-position change in one transaction
  ([`91025e2`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/91025e276702aeba9ea12cfde43c4059fb5e19f5))

- **rename-triggers**: Keep a module two levels below a retyped card unchanged
  ([`e081d73`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/e081d73097e0785dc03121fe9422c254253baec3))

- **rename-triggers**: Pin the exact rule reads of a type change and split its control
  ([`afdaa32`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/afdaa329b86c3e79f06d6a88747e33aabda9e99d))

- **rename-triggers**: Read the raw name of a nested module from NetBox
  ([`e913636`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/e91363612dca08ffa9b2987c39e44476fa524913))

- **rename-triggers**: Report the family a channelized rule built after a move without a rule
  ([`bee7fc5`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/bee7fc540961526bdac478f9dee9615dd5fcd680))

- **rename-triggers**: Report the family a channelized rule built after a move without a rule
  ([`64339cf`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/64339cf99295ca4ff8d4d5d2a4452fa563c0f59f))

- **rename-triggers**: Run saves before a device change through the shared helper
  ([`9e24938`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/9e249383a288378cd83d4550f046685ca6ee6dc1))

- **rename-triggers**: Share the chassis-change helpers and rule shapes
  ([`8e4eec2`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8e4eec24b81773e0530acef9707c39b1cdf9a903))

- **template-names**: Remove the tests of stored text that spells a former chassis-position marker
  ([`c524d46`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/c524d4699e05c4ff5cae577e701d4f75f093e4ab))

- **vc-drift**: Read the historical matchers from the resolved templates
  ([`ba3a3d9`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ba3a3d95e6ea12ceb602fd09490a80a812d84596))

- **vc-drift**: Read the historical matchers from the resolved templates
  ([`97d6d76`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/97d6d765daf6c0e99b3375fa62c116ec1cb85d54))


## v1.6.0 (2026-09-28)

### Bug Fixes

- **rename-triggers**: Keep earlier device-interface outcomes when a later family fails
  ([`cc3757c`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/cc3757cd5a2717c7aef5c5faceea32d3e3dc9975))


## v1.5.5 (2026-09-27)

### Bug Fixes

- **name-template**: Show the digits {slot} gives for a bay on the device
  ([`8ec4158`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8ec4158fd16227213e66dc9084a872d157e0959c))

### Chores

- **ruff**: Ban imports of the private slot description
  ([`02f92ff`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/02f92ff8416bd1c3ec7e5509492880d9db8f3d8e))

### Documentation

- **changelog**: File the 1.5.4 notes under their release and document the release flow
  ([`ea49dcb`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ea49dcb8826870449b57b32f9ba2d815fa94b08d))

- **contributing**: State that every PR merges with a merge commit
  ([`ba03c87`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ba03c872c2d98adf7832c959f238b6267c633edf))

- **contributing**: State what parse_squash_commits does for a squash release
  ([`df6f253`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/df6f253bd83b8351e870c94eea982170dfc6535a))

### Refactoring

- **raw-bases**: Read the parent template only when the rule applies it
  ([`284c120`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/284c1208624c3ba0bb4214f066e385e4b881e051))


## v1.5.4 (2026-09-26)

### Features

- Test and preview device-level rules in the Build Rule tester. Derive the port from
  the interface name, accept a virtual-chassis position, and retain the rule kind
  and interface-name filter when opening the add form. Module-rule previews now
  accept a `{vc_position}` value, and the module variable table lists it.

### Bug Fixes

- Add name-template language and device-interface rule support
  ([#114](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/114),
  [`eb2dda3`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/eb2dda313ace1c2080a4ee40adb4cfeaa0fc3fa9))

- The REST API now clears the rule-mode fields the same way the web form does. A
  device-level rule sent with `module_type_is_regex: true` no longer fails with a
  server error; it saves with regex mode off. A module-type rule sent with a
  `module_type_pattern` saves with the pattern cleared, instead of a 400 response.

- A rule save with `update_fields` now validates the row it stores. Fields outside
  `update_fields` come from the database, not from unsaved values on the instance.
  A flat rule can no longer store a `{channel}` template, and an unsaved invalid
  mode no longer blocks a valid template save.

- A name template variable that starts with a non-ASCII letter, such as `{é}`, is now
  read as a variable. Before, a group like `{é é}` passed the rule check and failed
  only when the rule renamed an interface, and `{é!r}` got the generic unsafe
  expression error instead of the format-field error.

- A device-interface rule can no longer store a Parent Module Type. Migration
  `0018` clears the field on every existing device-interface rule and logs each
  rule ID and cleared module type. The engine never matched a device rule on
  this field, so only the rule ranking changes. A rollback does not restore the
  cleared values. Record them before the upgrade if you need them.

- A save now refuses a brace group that can never evaluate, such as `{slot_num // 2}`,
  `{ channel }`, `{bay_position.x}`, or `{bay_position!r}`. Before, the rule saved and
  failed on every rename. The error quotes the group and shows the variable-token form,
  `{{slot_num} // 2}`. The name-template audit migration reports these groups too.

### Chores

- Fix import order ([#95](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/95),
  [`d6dde5a`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/d6dde5ac295bbad1813a2a13e1d72697c48dcbaf))

- **deps**: Bump codecov/codecov-action from 7.0.0 to 7.1.1 in the github-actions group
  ([#110](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/110),
  [`4a7b2fb`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/4a7b2fb9eef959db521eca7d8f6378d4ace2f40c))

- **deps**: Bump the uv group across 1 directory with 10 updates
  ([#94](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/94),
  [`2743e33`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2743e33510189ad060c378bc13400671a1930720))

- **deps-dev**: Bump django from 6.0.7 to 6.1.1
  ([#105](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/105),
  [`b67445f`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b67445f1bbfd572b52fe5272ce6748ca1cfece80))

- **deps-dev**: Bump pre-commit from 4.5.1 to 4.6.2
  ([#106](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/106),
  [`9cd75d8`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/9cd75d8e8f017441ebde0f465f308c4053d66032))

- **deps-dev**: Bump pytest from 9.0.3 to 9.1.1
  ([#107](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/107),
  [`b829039`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b8290393b63f1236e199f67f2c95862843978aed))

- **deps-dev**: Bump pytest-cov from 7.0.0 to 7.1.0
  ([#108](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/108),
  [`bf3fae8`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/bf3fae88ec65640208b36bad0cc78773d797aef0))

- **deps-dev**: Bump pytest-django from 4.12.0 to 4.14.0
  ([#109](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/109),
  [`39f9ec9`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/39f9ec99abce2d2ef4593a524237aa3696cd2459))


## v1.5.3 (2026-09-21)

### Bug Fixes

- Support composed bay positions and surface rules that match no m…
  ([#93](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/93),
  [`aeb1247`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/aeb1247b1cabe0e1511411aa4e466e62c90e4f95))

### Chores

- **deps**: Bump the github-actions group with 3 updates
  ([#91](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/91),
  [`6638f09`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/6638f09295b57e9a41340db3a7ba1742bbf497b0))

### Testing

- Isolate the suite per worker and enforce two conventions mechanically
  ([#86](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/86),
  [`c23614d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/c23614d0f3ec429ad1c0371c3f49f368e65e63fc))


## v1.5.2 (2026-09-09)

### Chores

- **deps**: Bump the github-actions group with 2 updates
  ([#85](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/85),
  [`8e409a7`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/8e409a7aded1307bb70f1c99aa6163fde4d26008))

- **deps**: Bump the github-actions group with 2 updates
  ([#84](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/84),
  [`49d51ca`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/49d51caf26e583929c4310e22e845d4aaa1c8a13))

### Refactoring

- Establish interface family architecture
  ([#83](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/83),
  [`47569b2`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/47569b29882191eb432fe19ace625966d87c134d))


## v1.5.1 (2026-08-22)

### Bug Fixes

- Preserve channel names after NetBox deferred cascade
  ([#71](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/71),
  [`0cc795a`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/0cc795a239aa459b603e77f56407930e139323ef))

### Chores

- **deps**: Bump the github-actions group across 1 directory with 3 updates
  ([#67](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/67),
  [`6319fe2`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/6319fe2637d90b490b64b8293161941b46cfab59))

- **deps**: Bump the github-actions group with 2 updates
  ([#69](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/69),
  [`36a1b03`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/36a1b03ef6bd1a557081f5f795884fd666937bd4))

- **deps**: Bump the github-actions group with 2 updates
  ([#68](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/68),
  [`e33bdaf`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/e33bdaf5bcf7e9166a807b54f88b2ee137dcf4bb))

- **deps**: Bump the github-actions group with 3 updates
  ([#70](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/70),
  [`becf341`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/becf3415e8af8cc4c5d56469599208236d4c51ab))


## v1.5.0 (2026-07-30)

### Chores

- Add CODEOWNERS to auto-request @marcinpsk on PRs
  ([`7c732f8`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/7c732f89f569cba2f1f2be2eacb347eb94bbb972))

- Cover .github/CODEOWNERS in REUSE.toml
  ([#53](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/53),
  [`ca82928`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ca82928289dc1f12367d5a2fed465c66bb3aeeef))

- **deps**: Bump the github-actions group across 1 directory with 2 updates
  ([#48](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/48),
  [`1626af2`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/1626af2e30e7b1f99820ee2a9372c42a612e2acc))

- **deps**: Bump the github-actions group with 2 updates
  ([#51](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/51),
  [`2ea4715`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2ea4715d22982e83b30d7f5a9c94b272a65e41ac))

- **deps**: Bump the github-actions group with 3 updates
  ([#52](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/52),
  [`741b8a0`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/741b8a09c4abf37067a96a036ff7b85fc56959c2))

- **deps**: Bump the github-actions group with 6 updates
  ([#54](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/54),
  [`ba2e926`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ba2e926f2614dc05d27aa2a84c551194586c289b))

- **devcontainer**: Durable debug-toolbar boot patches for Python 3.14
  ([#56](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/56),
  [`b028a66`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b028a66cc1158a1064ed2645f126e375647d195d))

### Continuous Integration

- Test NetBox 4.6.5 and main; adopt NetBox test mixins and fix what they found
  ([#57](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/57),
  [`b314fbd`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b314fbd03421e2e468f17758a513582a2c4935c2))

### Features

- Support NetBox 4.7 channelized interfaces (#55)
  ([#64](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/64),
  [`0968f33`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/0968f337d5734bfe60626bb593c968c382b60485))


## v1.4.3 (2026-06-29)

### Bug Fixes

- **engine**: Close unpinned rule-cache torn-read + review follow-ups to #49
  ([#50](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/50),
  [`1dd0a09`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/1dd0a09b2bd4a1a906cc22d89b688a1e1bef6ead))


## v1.4.2 (2026-06-26)

### Performance Improvements

- **engine**: Load the enabled rule set once and memoize find_matching_rule
  ([#49](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/49),
  [`7d79295`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/7d79295f38c6f56b9cb1b65453ad1087414577e2))


## v1.4.1 (2026-06-15)

### Bug Fixes

- Update tests, update interface name conflict, support isolated test db
  ([#47](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/47),
  [`2ca8086`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2ca8086bd19cca93e502e0b720aa4fcc02963940))

### Chores

- Rqworker autoreload ([#44](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/44),
  [`b9a3d87`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b9a3d87ce4699d21c4c7bd1dbf1fb0fb46bfbae5))

- **deps**: Bump the github-actions group with 2 updates
  ([#46](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/46),
  [`eefc835`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/eefc835d81693f3754d77c06aeff6755f5d8ae74))

- **deps**: Bump the github-actions group with 3 updates
  ([#45](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/45),
  [`4f97f2e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/4f97f2ee5f195c5b4bc17fde05a605f207fc9c03))


## v1.4.0 (2026-05-28)

### Chores

- **deps**: Bump github/codeql-action from 4.35.3 to 4.35.4 in the github-actions group
  ([#40](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/40),
  [`f10b003`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/f10b00377767bf8572ce62504659e476cb484505))

- **deps**: Bump github/codeql-action from 4.35.5 to 4.36.0 in the github-actions group
  ([#43](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/43),
  [`0a773ab`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/0a773abff110a031ee93345cc392513ff1cefca5))

- **deps**: Bump the github-actions group with 2 updates
  ([#41](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/41),
  [`65882d5`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/65882d5f4f2c255deffaf08ce0cab1d8c9f5036b))

### Features

- Dev fixes, added receiver for signal to provide mutated inteface name
  ([#42](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/42),
  [`43abd6f`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/43abd6f949c551657dc7dbed1b03046da6338cda))


## v1.3.1 (2026-05-13)

### Bug Fixes

- Replace deprecated CheckConstraint check= with condition= for Django 5.x compatibility
  ([`bd8b193`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/bd8b1937a6d59109ac6e824e41d42bd21e56c0fa))

### Chores

- **deps**: Bump astral-sh/setup-uv from 8.0.0 to 8.1.0 in the github-actions group
  ([#38](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/38),
  [`1cb9181`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/1cb918111375dfd9017f151ae467c479a45308c7))

- **deps**: Bump github/codeql-action from 4.35.2 to 4.35.3 in the github-actions group
  ([#39](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/39),
  [`2e49d35`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/2e49d35ec08b8e93203ce221b2f6e323d457eeff))

- **deps**: Bump pypa/gh-action-pypi-publish from 1.13.0 to 1.14.0 in the github-actions group
  ([#36](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/36),
  [`603b457`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/603b45757786fce28c3289ec36af41f05bcd3f65))

- **deps**: Bump the github-actions group with 2 updates
  ([#37](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/37),
  [`9e200a9`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/9e200a97a09b70264d3efb81099a623cf7714b06))

- **deps**: Bump the github-actions group with 3 updates
  ([#35](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/35),
  [`625ea99`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/625ea99de4815d77d9678632cd64b9cf261136c0))


## v1.3.0 (2026-03-31)

### Chores

- **deps**: Bump github/codeql-action from 4.33.0 to 4.34.1 in the github-actions group
  ([#33](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/33),
  [`f610cc5`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/f610cc5b7a3fd8816cdbc97edcf682abe0e6f396))

### Features

- Remove module path plus add YAML export option
  ([#34](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/34),
  [`66fbf7b`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/66fbf7b5ddb9f97b4c3ec2ba8f54d2e6800f132e))


## v1.2.3 (2026-03-21)

### Bug Fixes

- Harden engine, views, and signals from code review
  ([#32](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/32),
  [`1dfe0fc`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/1dfe0fc63f857b57ec9a272d4b3a4fa3f57234d7))

### Chores

- **deps**: Bump the github-actions group with 3 updates
  ([#31](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/31),
  [`157c3d5`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/157c3d50253fa15943749c758fac3289f409627d))

- **deps**: Bump the github-actions group with 3 updates
  ([#29](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/29),
  [`5962490`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5962490147391056e7e3f10f7ff02307577c7d1e))

- **deps-dev**: Update django requirement from <6.0,>=5.1 to >=5.1,<7.0
  ([#30](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/30),
  [`65496da`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/65496dad3e7b12765e9952f7b757ea42c50289dd))


## v1.2.2 (2026-03-12)

### Bug Fixes

- Added pyproject updates / ruff updates
  ([#28](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/28),
  [`cb526ca`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/cb526ca42c917a59c8503af8550a08eb2e412499))

### Chores

- Update dependabot
  ([`6fba7f2`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/6fba7f2ab227da5d669a3bd384141fe8f7d9aaa0))

- Update pyproject/__init__.py
  ([`5c78985`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5c7898582f8e542979b6f820f611df72504ba606))

- **deps**: Bump actions/upload-artifact from 6.0.0 to 7.0.0
  ([#22](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/22),
  [`386a38d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/386a38ddf352f2308ab5f3a53d5419ab15142f98))

- **deps**: Bump amannn/action-semantic-pull-request from 5.5.3 to 6.1.1
  ([#23](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/23),
  [`56ee2e4`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/56ee2e4d41361f37e93dc0a6c5cc2dc9c834a82d))

- **deps**: Bump the github-actions group with 4 updates
  ([#27](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/27),
  [`022ca6d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/022ca6d4b947f4af7cbc41056264f8ca92fcdb35))

### Testing

- Add targeted coverage tests to reach 97%
  ([#21](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/21),
  [`5ca1ce6`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5ca1ce6b79371df877f78b5f70050600c2a7e6f2))


## v1.2.1 (2026-03-01)

### Bug Fixes

- Exempt standalone E2E/data-loading scripts and forced bump
  ([#20](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/20),
  [`b0ecd29`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b0ecd291c5b3125fa719b440d67d7199687fcdf3))

### Continuous Integration

- Add Codecov PR comments and conventional commit PR title check
  ([#19](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/19),
  [`ccb6e38`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/ccb6e3822fd19df6aebceca1d69f5a4497273594))


## v1.2.0 (2026-03-01)

### Documentation

- Add DeepWiki link ([#16](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/16),
  [`7a8df9e`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/7a8df9e702b703aa510d1c6caee37018066ead50))

### Features

- Add VC support ([#18](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/pull/18),
  [`922391d`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/922391def56f93f9be81d76f9a28b9a8d624b96b))


## v1.1.2 (2026-02-24)

### Bug Fixes

- **ci**: Use PAT for semantic-release, decouple publish workflow
  ([`3197110`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/31971104e6c004f2d635ceb81195a8a7c15cf7ae))

- **docs**: Pin mkdocs <2 to avoid Material incompatibility
  ([`971900b`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/971900b4f17f5673c948b6c2445e1a9f5c887a0a))


## v1.1.1 (2026-02-24)

### Bug Fixes

- **ci**: Fetch tags explicitly after checkout
  ([`b89dfa0`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/b89dfa022010b25456bfd1ddd9fb89c073de1a02))

### Chores

- **deps**: Bump actions/upload-artifact from 4.6.2 to 6.0.0
  ([`5f890b5`](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/commit/5f890b533ccb5c43949926b7de1b5b29acbfdd8e))


## v1.0.0 (2026-02-24)

- Initial Release

## v1.0.0 (2026-02-20)

- Initial Release
