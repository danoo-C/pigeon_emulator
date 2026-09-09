#pragma origin 0x20000
#include <stdlibpigeon>

void special_math(int a, int b, int* c, int size_c){
    for(int i = 0; i<size_c; i++){
        if(i < size_c/2){
            c[i] = a * b + i ^ a
        }
        else if(i > size_c/2){
            c[i] = a * b + i ^ b
        }
    }
}

void even_more_math(int *c, int size_c, int trigger, int* handle_trigger_func){
    for(int i = 0; i<size_c; i++){
        if(c[i] > trigger){
            c[i] = handle_triger_func(c[i], i);
        }
    }
}

int trigger_func(int a, int i){
    return a - i;
}

int main(){
    int a = 10;
    int b = 20;
    int* c = malloc(100);

    spcial_math(a,b,c,100);
    even_more_math(c, 100, 512, &trigger_func);

    char* a = "math be like: "
    std_print_display(a, 0,0, 5,5);//n , x, y, fontx, fonty
    std_print_display(a, 0 ,6, 5,5);

    return 0;
}