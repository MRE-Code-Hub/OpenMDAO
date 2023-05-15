// <<hpp_insert gen/utils.js>>
// <<hpp_insert gen/defaults.js>>
// <<hpp_insert src/OmModelData.js>>
// <<hpp_insert src/OmDiagram.js>>

var sharedTransition = null;

var enterIndex = 0;
var exitIndex = 0;

// The modelData object is generated and populated by n2_viewer.py
let modelData = OmModelData.uncompressModel(compressedModel);
delete compressedModel;

var n2MouseFuncs = null;

function n2main() {
    const n2Diag = new OmDiagram(modelData);
    n2MouseFuncs = n2Diag.getMouseFuncs();

    n2Diag.update(false);

    if (initialPath) {
        node = n2Diag.layout.model.root.findNode(initialPath);
        if (node != undefined) {
            n2Diag.ui._setupLeftClick(node)
        }
    };
}

// wintest();
n2main();
