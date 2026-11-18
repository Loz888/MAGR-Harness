// Fixed Go parser: inspect source syntax, never import or execute project code.
package main

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strings"
)

type stringsSet map[string]bool

var supported = true

func merge(values ...stringsSet) stringsSet {
	out := stringsSet{}
	for _, value := range values {
		for key := range value {
			out[key] = true
		}
	}
	return out
}

func receiverName(expr ast.Expr) string {
	switch node := expr.(type) {
	case *ast.Ident:
		return node.Name
	case *ast.StarExpr:
		return receiverName(node.X)
	case *ast.IndexExpr:
		return receiverName(node.X)
	}
	return ""
}

func inspect(path string, callers []string, required string) bool {
	file, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
	if err != nil {
		supported = false
		return false
	}
	functions := map[string]*ast.FuncDecl{}
	for _, declaration := range file.Decls {
		fn, ok := declaration.(*ast.FuncDecl)
		if !ok {
			continue
		}
		name := fn.Name.Name
		if fn.Recv != nil && len(fn.Recv.List) == 1 {
			name = receiverName(fn.Recv.List[0].Type) + "." + name
		}
		if functions[name] != nil {
			return false
		}
		functions[name] = fn
	}
	available := stringsSet{}
	for name := range functions {
		selected := false
		for _, caller := range callers {
			selected = selected || name == caller
		}
		if !selected {
			available[name] = true
		}
	}
	common := merge(available)
	for _, name := range callers {
		fn := functions[name]
		if fn == nil || fn.Body == nil {
			return false
		}
		ast.Inspect(fn.Body, func(node ast.Node) bool {
			switch node.(type) {
			case *ast.IfStmt, *ast.ForStmt, *ast.RangeStmt, *ast.SwitchStmt, *ast.SelectStmt,
				*ast.FuncLit, *ast.GoStmt, *ast.DeferStmt:
				supported = false
			}
			return true
		})
		if !supported {
			return false
		}
		environment := map[string]stringsSet{}
		receiver := ""
		if fn.Recv != nil && len(fn.Recv.List) == 1 {
			receiver = receiverName(fn.Recv.List[0].Type)
			for _, item := range fn.Recv.List[0].Names {
				environment[item.Name] = stringsSet{"@input": true}
			}
		}
		if fn.Type.Params != nil {
			for _, field := range fn.Type.Params.List {
				for _, item := range field.Names {
					environment[item.Name] = stringsSet{"@input": true}
				}
			}
		}
		var expression func(ast.Expr) stringsSet
		expression = func(expr ast.Expr) stringsSet {
			switch node := expr.(type) {
			case *ast.Ident:
				if _, exists := environment[node.Name]; !exists && functions[node.Name] != nil {
					return stringsSet{"@func:" + node.Name: true}
				}
				return environment[node.Name]
			case *ast.SelectorExpr:
				return expression(node.X)
			case *ast.CallExpr:
				values := stringsSet{}
				for _, argument := range node.Args {
					values = merge(values, expression(argument))
				}
				callee := ""
				switch target := node.Fun.(type) {
				case *ast.Ident:
					for key := range expression(target) {
						if strings.HasPrefix(key, "@func:") {
							callee = strings.TrimPrefix(key, "@func:")
						}
					}
				case *ast.SelectorExpr:
					if expression(target.X)["@input"] {
						callee = receiver + "." + target.Sel.Name
					}
				}
				if values["@input"] && available[callee] {
					values[callee] = true
				} else if values["@input"] {
					hasHelper := false
					for key := range values {
						hasHelper = hasHelper || available[key]
					}
					if !hasHelper {
						values["@outside-transform"] = true
					}
				}
				return values
			case *ast.ParenExpr:
				return expression(node.X)
			case *ast.IndexExpr:
				return merge(expression(node.X), expression(node.Index))
			case *ast.SliceExpr:
				return expression(node.X)
			case *ast.BinaryExpr:
				return merge(expression(node.X), expression(node.Y))
			case *ast.UnaryExpr:
				return expression(node.X)
			}
			return stringsSet{}
		}
		returns := []stringsSet{}
		var statements func([]ast.Stmt)
		statements = func(body []ast.Stmt) {
			for _, statement := range body {
				switch node := statement.(type) {
				case *ast.AssignStmt:
					for index, left := range node.Lhs {
						if variable, ok := left.(*ast.Ident); ok && index < len(node.Rhs) {
							environment[variable.Name] = expression(node.Rhs[index])
						}
					}
				case *ast.DeclStmt:
					if decl, ok := node.Decl.(*ast.GenDecl); ok {
						for _, part := range decl.Specs {
							if spec, ok := part.(*ast.ValueSpec); ok {
								for index, variable := range spec.Names {
									if index < len(spec.Values) {
										environment[variable.Name] = expression(spec.Values[index])
									}
								}
							}
						}
					}
				case *ast.ReturnStmt:
					if len(node.Results) != 1 {
						returns = append(returns, stringsSet{})
						continue
					}
					returns = append(returns, expression(node.Results[0]))
				case *ast.IfStmt:
					statements(node.Body.List)
					switch other := node.Else.(type) {
					case *ast.BlockStmt:
						statements(other.List)
					case *ast.IfStmt:
						statements([]ast.Stmt{other})
					}
				case *ast.BlockStmt:
					statements(node.List)
				case *ast.ForStmt:
					statements(node.Body.List)
				case *ast.RangeStmt:
					statements(node.Body.List)
				}
			}
		}
		statements(fn.Body.List)
		if len(returns) == 0 {
			return false
		}
		for _, flow := range returns {
			if flow["@outside-transform"] {
				return false
			}
			for key := range common {
				if !flow[key] {
					delete(common, key)
				}
			}
		}
	}
	if required != "" {
		for key := range common {
			if key != required && !strings.HasSuffix(key, "."+required) {
				delete(common, key)
			}
		}
	}
	return len(common) == 1
}

func main() {
	args := os.Args[1:]
	if len(args) > 0 && args[0] == "--" {
		args = args[1:]
	}
	if len(args) != 3 {
		os.Exit(2)
	}
	var callers []string
	if json.Unmarshal([]byte(args[1]), &callers) != nil {
		os.Exit(2)
	}
	passed := inspect(args[0], callers, args[2])
	json.NewEncoder(os.Stdout).Encode(map[string]bool{"passed": passed, "supported": supported})
}
